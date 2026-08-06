import asyncio
import json

import httpx
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from time import monotonic

from . import db as dbmod
from . import issue_tasks as it
from .clients.github import FastForwardError
from .clients.tg_card import run_title
from .e2e import (
    E2E_DIR,
    MAX_VIDEO_BYTES,
    E2EVerdict,
    E2EVerdictError,
    build_e2e_fix_prompt,
    build_e2e_prompt,
    e2e_report_dict,
    extract_from_zip,
    format_e2e_comment,
    parse_e2e_verdict,
    select_video_paths,
)
from .jsonextract import find_json_object
from .loopconfig import (
    LoopConfigError,
    find_spec_plan_pair,
    parse_loop_config,
    resolve_base_branch,
)
from .models import (
    AWAITING_APPROVAL,
    CANCELLED,
    DONE,
    E2E_TESTING,
    EXECUTING,
    FAILED,
    PLANNING,
    PREPARING,
    PUBLISHING,
    QUEUED,
    REPORTING,
    REVIEWING,
    STAGING,
    Run,
)
from .planning import (
    PlannerResult,
    PlanningError,
    build_advisor_prompt,
    build_planner_prompt,
    build_planner_revise_prompt,
    parse_advisor_verdict,
    parse_planner_output,
    plan_paths,
)
from .review import (
    Finding,
    VerdictError,
    build_fix_prompt,
    build_review_prompt,
    format_review_comment,
    newly_fixed,
    parse_verdict,
    report_dict,
)
from .secrets import (
    SECRETS_FILE,
    SECRETS_GITIGNORE,
    SOURCE_LINE,
    load_repo_secrets,
    render_env_file,
    source_hint,
)
from .state_machine import InvalidTransition, transition

RATE_LIMIT_MARKERS = ("rate limit", "usage limit", "limit reached")

# Claude Code surfaces API-level failures ("API Error: Response stalled
# mid-stream", dropped connections) by failing the whole task, but the agent's
# session in the sandbox survives — resubmitting with continue_session picks
# the work up where it stopped instead of losing the stage. Checked after
# RATE_LIMIT_MARKERS, which need the long pause, not an instant resume.
TRANSIENT_AGENT_MARKERS = ("api error", "connection error", "econnreset",
                           "socket hang up", "fetch failed")

MAX_TASK_TIMEOUT_S = 86400


class RunFailure(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


class ExecutionTimeout(Exception):
    """Agent task used up run.timeout_minutes of actual working time."""


class ReviewTaskError(Exception):
    """The review or fix task failed for a non-rate-limit reason."""


class ReviewDeadline(Exception):
    """The run's review time budget ran out."""


class SyncError(Exception):
    """Conflict resolution could not deliver a merged PR branch."""


CONTINUE_PROMPT = "Continue the previous task from where it stopped."

PREVIEW_TASK_TIMEOUT_S = 600

SYNC_TASK_TIMEOUT_S = 1800


def sync_app_name(run: Run) -> str:
    repo_short = run.repo.split("/")[-1][:20]
    return f"loop-{repo_short}-pr{run.pr_number}-sync-r{run.id}"


def build_sync_prompt(repo: str, base_ref: str) -> str:
    return (
        "The branch checked out here is a pull request branch that conflicts "
        f"with its base branch `{base_ref}` and cannot be merged.\n"
        f"Load the fetch credential first: `{SOURCE_LINE}` — it defines "
        "GIT_SYNC_TOKEN. Never print it or write it to a file.\n"
        "Then fetch the base branch:\n"
        f"  git fetch https://x-access-token:${{GIT_SYNC_TOKEN}}@github.com/{repo}.git {base_ref}\n"
        "Run `git merge FETCH_HEAD` and resolve every conflict preserving "
        "the intent of BOTH sides: in append-style files (journals, changelogs, "
        "logs) keep both entries in order; in code make the two changes "
        "compose — never drop either side. Conclude the merge with "
        "`git add -A && git commit --no-edit`. Do not push. Do not switch "
        "branches. Do not amend or rebase existing commits.\n\n"
        "Your FINAL message must be a single JSON object and nothing else:\n"
        '{"resolved": true|false, "files": ["path", ...], "notes": "one line"}\n'
        'Set "resolved": false only if the conflict cannot be resolved '
        "faithfully; explain why in notes."
    )


def build_preview_prompt(run_cmd: str) -> str:
    return (
        "Start the app's web server so a human can try it in a browser.\n"
        f"Run `{run_cmd}` in the background (e.g. with nohup), wait until it "
        "responds on its port, and finish with a one-line confirmation. "
        "Do not stop the server before finishing."
    )


def app_name(run: Run) -> str:
    repo_short = run.repo.split("/")[-1][:20]
    return f"loop-{repo_short}-pr{run.pr_number}-r{run.id}"


def planning_app_name(run: Run) -> str:
    repo_short = run.repo.split("/")[-1][:20]
    return f"loop-{repo_short}-i{run.issue_number}-r{run.id}"


def build_prompt(spec_path: str, plan_path: str, test_cmd: str | None,
                 setup_cmd: str | None = None,
                 secrets: dict[str, str] | None = None) -> str:
    setup_line = (
        f"First install the project dependencies with `{setup_cmd}`.\n"
        if setup_cmd else ""
    )
    test_line = (
        f"Before finishing, run the tests with `{test_cmd}` — they must pass.\n"
        if test_cmd else ""
    )
    return (
        "You are executing a prepared feature plan in this repository.\n"
        f"Specification: {spec_path}\n"
        f"Plan: {plan_path}\n\n"
        + source_hint(secrets or {}) + setup_line +
        "Read both files and complete every task of the plan in order "
        "(use the parallel-plan-execution skill if it is available). "
        "Tick off completed tasks directly in the plan file. "
        "Make a git commit after each completed task. "
        "Do not git push — publishing is handled by an external system. "
        "Do not switch branches.\n"
        + test_line +
        "Finish with a short summary: what was done, what was verified, what failed."
    )


class Pipeline:
    def __init__(self, db, settings, gh, sb, tg):
        self.db = db
        self.settings = settings
        self.gh = gh
        self.sb = sb
        self.tg = tg

    async def _prepare(self, run: Run) -> None:
        raw = await self.gh.get_file(run.repo, run.head_branch, ".loop.yml")
        if raw is None:
            raise RunFailure(PREPARING, "no .loop.yml in the repository")
        try:
            cfg = parse_loop_config(raw)
        except LoopConfigError as e:
            raise RunFailure(PREPARING, f".loop.yml is invalid: {e}") from e

        files = await self.gh.list_pr_files(run.repo, run.pr_number)
        try:
            run.spec_path, run.plan_path = find_spec_plan_pair(files, cfg)
        except LoopConfigError as e:
            raise RunFailure(PREPARING, str(e)) from e

        run.timeout_minutes = cfg.timeout_minutes or self.settings.default_timeout_minutes
        run.test_cmd = cfg.test
        run.review_enabled = cfg.review_enabled
        run.review_max_iterations = (
            cfg.review_max_fix_iterations
            if cfg.review_max_fix_iterations is not None
            else self.settings.review_max_fix_iterations)
        run.approval_mode = cfg.approval

        if cfg.e2e_services:
            raise RunFailure(PREPARING, "e2e.services is not supported yet")
        run.e2e_enabled = cfg.e2e_enabled
        if cfg.e2e_enabled and not cfg.run and not cfg.e2e_env:
            raise RunFailure(
                PREPARING,
                "e2e is enabled but there is neither a run command nor e2e.env")
        run.run_cmd = cfg.run
        run.e2e_env_json = json.dumps(cfg.e2e_env) if cfg.e2e_env else None
        run.e2e_max_iterations = (
            cfg.e2e_max_fix_iterations
            if cfg.e2e_max_fix_iterations is not None
            else self.settings.e2e_max_fix_iterations)

        repo_secrets = load_repo_secrets(self.settings.secrets_dir, run.repo)
        missing = [k for k in cfg.required_env if k not in repo_secrets]
        if missing:
            raise RunFailure(PREPARING, "missing project secrets: " + ", ".join(missing))
        run.prompt = build_prompt(run.spec_path, run.plan_path, cfg.test, cfg.setup,
                                  repo_secrets)

        # Fresh clone per run: previous runs' apps for this PR are stale.
        for old_app in await dbmod.previous_app_ids(self.db, run.repo, run.pr_number, run.id):
            await self.sb.delete_app(old_app)

        run.app_id = await self.sb.create_app(
            name=app_name(run),
            repo_url=f"https://github.com/{run.repo}.git",
            branch=run.head_branch,
            credential_id=self.settings.git_credential_id,
            preset=cfg.sandbox_preset,
        )
        await dbmod.save_run(self.db, run)
        for key, value in repo_secrets.items():
            await self.sb.set_app_secret(run.app_id, key, value)
        run.sandbox_id = await self.sb.create_sandbox(run.app_id)
        await dbmod.save_run(self.db, run)
        await self._write_secrets(run, repo_secrets)

    async def _write_secrets(self, run: Run, secrets: dict[str, str]) -> None:
        """Drop the run's secrets into the sandbox as a sourceable env file.

        Fatal on failure: a stage that silently runs without its credentials
        fails later and far less legibly than here.
        """
        if not secrets:
            return
        try:
            await self.sb.put_file(run.sandbox_id, SECRETS_GITIGNORE, "*\n")
            await self.sb.put_file(run.sandbox_id, SECRETS_FILE,
                                   render_env_file(secrets))
        except Exception as e:  # noqa: BLE001
            raise RunFailure(
                run.state,
                f"could not place the project secrets in the sandbox: {e}") from e

    async def _submit_resumable(self, run: Run, prompt: str, timeout_s: int,
                                model: str | None = None,
                                continue_session: bool = False) -> str:
        """Submit a stage task, tolerating a sandbox that cannot accept it yet.

        sandboxd answers 409 while the sandbox is occupied or not ready: after
        an orchestrator restart the pre-restart task may still run (one task
        at a time — wait it out), and a freshly created sandbox may still be
        seeding its workspace (the first import of a new repo outlives
        sandbox creation, seen live on run #24). Retry until the deadline.
        """
        deadline = monotonic() + timeout_s
        while True:
            try:
                return await self.sb.submit_task(run.sandbox_id, prompt,
                                                 timeout_s=timeout_s, model=model,
                                                 continue_session=continue_session)
            except httpx.HTTPStatusError as e:
                if monotonic() >= deadline:
                    raise
                # Anything other than "busy" is only worth retrying if the
                # sandbox turned out to be asleep and we just woke it.
                if e.response.status_code != 409 and not await self._ensure_awake(run):
                    raise
            await self._drain_stale_task(run, max(0.0, deadline - monotonic()))
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _ensure_awake(self, run: Run) -> bool:
        """Restart the sandbox if the idle reaper stopped it. True if it did.

        The reaper stops containers, it does not delete them: the workspace and
        the agent's session survive, so a stopped sandbox is worth waking
        rather than declaring the run dead. Seen live on run #40, whose
        sandbox was reaped while the stage sat out a rate-limit pause.
        """
        try:
            info = await self.sb.get_sandbox(run.sandbox_id)
        except httpx.HTTPError:
            return False
        if info.get("status") in (None, "running"):
            return False
        return await self.sb.start_sandbox(run.sandbox_id)

    async def _poll_wait(self, run: Run) -> None:
        """Wait one poll interval while holding the sandbox awake.

        Every polling loop goes through here: sandboxd's idle reaper counts an
        agent task as inactivity (see SandboxdClient.keepalive), so a stage that
        outlasts the instance threshold dies unless something keeps refreshing.
        """
        await self.sb.keepalive(run.sandbox_id, self.settings.keepalive_minutes)
        await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _sleep_awake(self, run: Run, seconds: float) -> None:
        """Sleep out a pause while holding the sandbox awake.

        A rate-limit pause (an hour by default) outlives both the keepalive
        window and sandboxd's idle threshold, and nothing polls during it — a
        plain sleep hands the reaper a sandbox to stop and the resume then
        lands in a dead one. Refresh twice per window instead.
        """
        step = max(60.0, self.settings.keepalive_minutes * 60 / 2)
        remaining = seconds
        while remaining > 0:
            await self.sb.keepalive(run.sandbox_id, self.settings.keepalive_minutes)
            await asyncio.sleep(min(step, remaining))
            remaining -= step
        # A keepalive lost to a restart or an older sandboxd still leaves the
        # sandbox reapable; the resume that follows deserves a live one.
        await self._ensure_awake(run)

    async def _task_status(self, run: Run, task_id: str) -> dict | None:
        """Read a task, or None when the control plane is briefly unreachable.

        A poll tick is not a checkpoint: skipping one costs a poll interval,
        while raising kills a run whose agent is still working in its sandbox.
        Docker's embedded DNS starts timing out when the host is loaded (runs
        #41 and #42 both died on EAI_AGAIN while their planners carried on
        planning), and with_retries' few seconds are nowhere near that outage.
        The stage deadline, which every caller re-checks each turn, stays the
        bound on waiting.
        """
        try:
            return await self.sb.get_task(run.sandbox_id, task_id)
        except httpx.TransportError:
            return None
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                return None
            raise

    async def _drain_stale_task(self, run: Run, timeout_s: int) -> None:
        try:
            tasks = await self.sb.list_tasks(run.sandbox_id)
        except httpx.TransportError:
            return  # unreachable control plane: the caller retries anyway
        stale = next((t for t in tasks if t.get("status") == "running"), None)
        if stale is None:
            return
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            task = await self._task_status(run, stale["id"])
            if task is not None and task.get("status") != "running":
                return
            await self._poll_wait(run)

    async def _execute(self, run: Run) -> None:
        timeout_s = min(run.timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        if not run.task_id:
            run.task_id = await self._submit_resumable(run, run.prompt, timeout_s)
            await dbmod.save_run(self.db, run)
        # The budget covers agent working time only; waiting out subscription limits
        # is not work, so every rate-limit pause pushes the deadline forward.
        deadline = monotonic() + timeout_s
        rate_limit_attempts = 0
        transient_attempts = 0
        while True:
            if monotonic() >= deadline:
                raise ExecutionTimeout
            task = await self._task_status(run, run.task_id)
            if task is None:
                await self._poll_wait(run)
                continue
            status = task.get("status")
            if status == "running":
                await self._poll_wait(run)
                continue
            if status == "succeeded":
                # GET /v1/sandboxes/{id}/tasks/{taskId} serialises TaskResult, whose
                # field is agent_message_final; agent_message is the list-endpoint name.
                run.summary = (task.get("agent_message_final")
                               or task.get("agent_message") or "(no summary)")
                await dbmod.save_run(self.db, run)
                return
            blob = " ".join(filter(None, (
                task.get("error_message"), task.get("failure_reason"),
                task.get("agent_message_final"), task.get("agent_message"),
            ))).lower()
            if (status == "failed" and rate_limit_attempts < 3
                    and any(m in blob for m in RATE_LIMIT_MARKERS)):
                rate_limit_attempts += 1
                await self.tg.send(
                    f"⏳ Run #{run.id}: hit the subscription rate limit, resuming in "
                    f"{self.settings.rate_limit_retry_minutes} min "
                    f"(attempt {rate_limit_attempts}/3).",
                    thread_id=run.tg_thread_id)
                paused_at = monotonic()
                await self._sleep_awake(run, self.settings.rate_limit_retry_minutes * 60)
                deadline += monotonic() - paused_at
                run.task_id = await self.sb.submit_task(
                    run.sandbox_id, "Continue executing the plan from where you stopped.",
                    timeout_s=timeout_s, continue_session=True)
                await dbmod.save_run(self.db, run)
                continue
            if (status == "failed"
                    and transient_attempts < self.settings.agent_retry_attempts
                    and any(m in blob for m in TRANSIENT_AGENT_MARKERS)):
                transient_attempts += 1
                await dbmod.add_event(
                    self.db, run.id, EXECUTING, EXECUTING,
                    f"transient agent error, resuming the session (attempt "
                    f"{transient_attempts}/{self.settings.agent_retry_attempts}): "
                    f"{task.get('error_message') or 'no details'}")
                # Stalls cluster during provider incidents; give the outage a
                # moment to pass instead of resuming straight into it.
                await self._sleep_awake(run, self.settings.agent_retry_backoff_seconds)
                run.task_id = await self.sb.submit_task(
                    run.sandbox_id, "Continue executing the plan from where you stopped.",
                    timeout_s=timeout_s, continue_session=True)
                await dbmod.save_run(self.db, run)
                continue
            raise RunFailure(
                EXECUTING,
                f"task finished with status {status}: "
                f"{task.get('error_message') or 'no details'}")

    async def _run_sandbox_task(self, run: Run, prompt: str, timeout_s: int,
                                deadline: float, model: str | None = None,
                                continue_session: bool = False) -> tuple[dict, float]:
        """Submit a task and poll it to completion within the given deadline.

        Subscription rate-limit pauses extend the deadline (waiting is not work).
        Returns (final task dict, possibly-extended deadline).
        """
        task_id = await self._submit_resumable(run, prompt, timeout_s, model=model,
                                               continue_session=continue_session)
        rate_limit_attempts = 0
        transient_attempts = 0
        while True:
            if monotonic() >= deadline:
                await self.sb.cancel_task(run.sandbox_id, task_id)
                raise ReviewDeadline
            task = await self._task_status(run, task_id)
            if task is None:
                await self._poll_wait(run)
                continue
            status = task.get("status")
            if status == "running":
                await self._poll_wait(run)
                continue
            if status == "succeeded":
                return task, deadline
            blob = " ".join(filter(None, (
                task.get("error_message"), task.get("failure_reason"),
                task.get("agent_message_final"), task.get("agent_message"),
            ))).lower()
            if (status == "failed" and rate_limit_attempts < 3
                    and any(m in blob for m in RATE_LIMIT_MARKERS)):
                rate_limit_attempts += 1
                await self.tg.send(
                    f"⏳ Run #{run.id}: hit the subscription rate limit, resuming in "
                    f"{self.settings.rate_limit_retry_minutes} min "
                    f"(attempt {rate_limit_attempts}/3).",
                    thread_id=run.tg_thread_id)
                paused_at = monotonic()
                await self._sleep_awake(run, self.settings.rate_limit_retry_minutes * 60)
                deadline += monotonic() - paused_at
                task_id = await self.sb.submit_task(
                    run.sandbox_id, CONTINUE_PROMPT,
                    timeout_s=timeout_s, continue_session=True, model=model)
                continue
            if (status == "failed"
                    and transient_attempts < self.settings.agent_retry_attempts
                    and any(m in blob for m in TRANSIENT_AGENT_MARKERS)):
                transient_attempts += 1
                await dbmod.add_event(
                    self.db, run.id, run.state, run.state,
                    f"transient agent error, resuming the session (attempt "
                    f"{transient_attempts}/{self.settings.agent_retry_attempts}): "
                    f"{task.get('error_message') or 'no details'}")
                # Same backoff as _execute: don't resume into the same outage.
                await self._sleep_awake(run, self.settings.agent_retry_backoff_seconds)
                task_id = await self.sb.submit_task(
                    run.sandbox_id, CONTINUE_PROMPT,
                    timeout_s=timeout_s, continue_session=True, model=model)
                continue
            raise ReviewTaskError(
                f"task finished with status {status}: "
                f"{task.get('error_message') or 'no details'}")

    async def _finish_review(self, run: Run, status: str, summary: str,
                             fixed: list[Finding], remaining: list[Finding]) -> None:
        run.review_status = status
        run.review_json = json.dumps(report_dict(summary, fixed, remaining),
                                     ensure_ascii=False)
        await dbmod.save_run(self.db, run)
        await dbmod.add_event(self.db, run.id, REVIEWING, REVIEWING,
                              f"review finished: {status}")

    async def _review(self, run: Run) -> None:
        # Reviewing gets a fresh run.timeout_minutes budget for the whole
        # review+fix cycle (execute's elapsed time is not persisted).
        deadline = monotonic() + run.timeout_minutes * 60
        review_timeout_s = min(self.settings.review_timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        fix_timeout_s = min(run.timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        fixed: list[Finding] = []
        pending: list[Finding] = []
        retried = False
        while True:
            try:
                task, deadline = await self._run_sandbox_task(
                    run, build_review_prompt(run.spec_path, run.plan_path, run.head_branch),
                    review_timeout_s, deadline, model=self.settings.reviewer_model)
                verdict = parse_verdict(task.get("agent_message_final")
                                        or task.get("agent_message") or "")
            except ReviewDeadline:
                await self._finish_review(run, "escalated",
                                          "review interrupted by run timeout",
                                          fixed, pending)
                return
            except (ReviewTaskError, VerdictError) as e:
                if retried:
                    await self._finish_review(run, "skipped",
                                              f"review skipped: {e}", fixed, pending)
                    return
                retried = True
                await dbmod.add_event(self.db, run.id, REVIEWING, REVIEWING,
                                      f"review attempt failed, retrying once: {e}")
                continue
            retried = False
            fixed += newly_fixed(pending, verdict.findings)
            pending = verdict.findings
            if verdict.verdict == "clean":
                await self._finish_review(run, "clean", verdict.summary, fixed, [])
                return
            if run.review_iteration >= run.review_max_iterations:
                await self._finish_review(run, "escalated", verdict.summary,
                                          fixed, pending)
                return
            run.review_iteration += 1
            await dbmod.save_run(self.db, run)
            await dbmod.add_event(self.db, run.id, REVIEWING, REVIEWING,
                                  f"fix iteration {run.review_iteration}: "
                                  f"{len(pending)} finding(s)")
            try:
                _, deadline = await self._run_sandbox_task(
                    run, build_fix_prompt(verdict, run.test_cmd),
                    fix_timeout_s, deadline)
            except ReviewDeadline:
                await self._finish_review(run, "escalated",
                                          "review interrupted by run timeout",
                                          fixed, pending)
                return
            except ReviewTaskError as e:
                await self._finish_review(run, "escalated",
                                          f"fix task failed: {e}", fixed, pending)
                return

    async def _finish_e2e(self, run: Run, status: str, summary: str,
                          verdict: E2EVerdict | None) -> None:
        run.e2e_status = status
        run.e2e_json = json.dumps(e2e_report_dict(summary, verdict), ensure_ascii=False)
        await dbmod.save_run(self.db, run)
        await dbmod.add_event(self.db, run.id, E2E_TESTING, E2E_TESTING,
                              f"e2e finished: {status}")

    async def _e2e(self, run: Run) -> None:
        # Like _review: a fresh run.timeout_minutes budget covers the whole
        # e2e+fix cycle; rate-limit pauses extend the deadline.
        deadline = monotonic() + run.timeout_minutes * 60
        task_timeout_s = min(run.timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        env = json.loads(run.e2e_env_json) if run.e2e_env_json else {}
        # Re-read rather than persist: secrets belong on the sandbox's disk and
        # in the server-side file, never in the run record.
        prompt = build_e2e_prompt(
            run.spec_path, run.run_cmd, env,
            load_repo_secrets(self.settings.secrets_dir, run.repo))
        retried = False
        while True:
            try:
                task, deadline = await self._run_sandbox_task(
                    run, prompt, task_timeout_s, deadline,
                    model=self.settings.e2e_model or None)
                verdict = parse_e2e_verdict(task.get("agent_message_final")
                                            or task.get("agent_message") or "")
            except ReviewDeadline:
                await self._finish_e2e(run, "escalated",
                                       "e2e interrupted by run timeout", None)
                return
            except (ReviewTaskError, E2EVerdictError) as e:
                if retried:
                    await self._finish_e2e(run, "skipped", f"e2e skipped: {e}", None)
                    return
                retried = True
                await dbmod.add_event(self.db, run.id, E2E_TESTING, E2E_TESTING,
                                      f"e2e attempt failed, retrying once: {e}")
                continue
            retried = False
            if verdict.verdict == "passed":
                await self._finish_e2e(run, "passed", verdict.summary, verdict)
                return
            if run.e2e_iteration >= run.e2e_max_iterations:
                await self._finish_e2e(run, "escalated", verdict.summary, verdict)
                return
            run.e2e_iteration += 1
            await dbmod.save_run(self.db, run)
            failing = sum(1 for t in verdict.tests if t.status == "failed")
            await dbmod.add_event(self.db, run.id, E2E_TESTING, E2E_TESTING,
                                  f"e2e fix iteration {run.e2e_iteration}: "
                                  f"{failing} failing test(s)")
            try:
                _, deadline = await self._run_sandbox_task(
                    run, build_e2e_fix_prompt(verdict, run.test_cmd),
                    task_timeout_s, deadline)
            except ReviewDeadline:
                await self._finish_e2e(run, "escalated",
                                       "e2e interrupted by run timeout", verdict)
                return
            except ReviewTaskError as e:
                await self._finish_e2e(run, "escalated",
                                       f"fix task failed: {e}", verdict)
                return

    async def _stage(self, run: Run) -> bool:
        """Commit and push the agent's work to the temp branch.

        Returns False when the agent made no commits (nothing to stage).
        """
        await self.sb.git_commit(run.app_id, message=f"loop: run #{run.id} leftovers")
        branch = f"loop/run-{run.id}"
        dropped = await self.sb.sanitize_git_config(run.sandbox_id)
        if dropped:
            await dbmod.add_event(
                self.db, run.id, run.state, run.state,
                "dropped repo-local git config the push audit rejects: "
                + ", ".join(dropped))
        push = await self.sb.git_push(run.app_id, branch)
        if not push.get("pushed"):
            if push.get("reason") == "no_local_commits":
                run.summary = ((run.summary or "") +
                               "\n\n⚠️ The agent made no code changes — "
                               "nothing to publish.").strip()
                await dbmod.save_run(self.db, run)
                return False
            raise RunFailure(STAGING, f"push rejected by sandboxd: {push.get('reason')}")
        run.staging_branch = branch
        await dbmod.save_run(self.db, run)
        return True

    async def _publish_ff(self, run: Run) -> None:
        if not run.staging_branch:
            return  # nothing was staged
        sha = await self.gh.branch_sha(run.repo, run.staging_branch)
        try:
            await self.gh.fast_forward(run.repo, run.head_branch, sha)
        except FastForwardError as e:
            raise RunFailure(
                PUBLISHING,
                f"the PR branch moved ahead, fast-forward is impossible; "
                f"the code is preserved in branch {run.staging_branch}") from e
        await self.gh.delete_branch(run.repo, run.staging_branch)

    async def _publish_plan(self, run: Run) -> None:
        if not await self._stage(run):
            raise RunFailure(PUBLISHING, "the planner produced no commits")
        sha = await self.gh.branch_sha(run.repo, run.staging_branch)
        try:
            await self.gh.fast_forward(run.repo, run.head_branch, sha)
        except FastForwardError as e:
            raise RunFailure(
                PUBLISHING,
                f"the issue branch moved ahead, fast-forward is impossible; "
                f"the plan is preserved in branch {run.staging_branch}") from e
        await self.gh.delete_branch(run.repo, run.staging_branch)
        run.staging_branch = None
        # Same branch scheduler.bootstrap forked the issue branch from, so the
        # PR's diff is exactly the plan and not everything base is missing.
        base = await resolve_base_branch(self.gh, run.repo)
        title = run.pr_title or f"loop: issue #{run.issue_number}"
        body = (f"Closes #{run.issue_number}.\n\n"
                f"Automated plan for issue #{run.issue_number} — see "
                f"`{run.spec_path}` and `{run.plan_path}`.\n\n"
                f"{run.summary or ''}").strip()
        run.pr_number = await self.gh.create_pr(
            run.repo, head=run.head_branch, base=base, title=title, body=body)
        await dbmod.save_run(self.db, run)

    async def _report_planning(self, run: Run) -> None:
        if run.pr_number == 0:  # questions outcome — no PR was created
            await self.gh.create_comment(
                run.repo, run.issue_number,
                "❓ Loop planner needs more information before it can plan "
                "this issue. Please answer in a comment; the task will resume "
                f"automatically.\n\n{run.summary or ''}")
            await it.set_state(self.db, run.repo, run.issue_number,
                               it.NEEDS_INFO)
            await self.tg.send(
                f"❓ Issue #{run.issue_number} ({run.repo}): the planner needs "
                "more information — reply in the issue to resume.",
                thread_id=run.tg_thread_id)
            return
        await self.gh.ensure_labels(run.repo)
        await self.gh.create_comment(
            run.repo, run.issue_number,
            f"🧭 Plan for issue #{run.issue_number} is ready and approved by "
            f"the Implementor Advisor: see PR #{run.pr_number}. "
            "Execution starts automatically.")
        await self.tg.send(
            f"🧭 Issue #{run.issue_number}: plan approved — PR "
            f"#{run.pr_number} queued for execution.",
            thread_id=run.tg_thread_id)
        await self.gh.add_labels(run.repo, run.pr_number, ["loop:run"])

    async def rescue_to_staging(self, run: Run) -> bool:
        """Best-effort push of whatever was committed; the PR branch is untouched."""
        try:
            return await self._stage(run)
        except Exception:  # noqa: BLE001
            return False

    async def _publish_partial(self, run: Run) -> None:
        try:
            if await self._stage(run):
                await self._publish_ff(run)
        except Exception:  # noqa: BLE001 — best-effort rescue of partial progress
            pass

    async def _start_preview(self, run: Run) -> None:
        """Best-effort: bring the web server up and record the sandbox preview URL."""
        if not run.run_cmd:
            return
        try:
            task_id = await self.sb.submit_task(
                run.sandbox_id, build_preview_prompt(run.run_cmd),
                timeout_s=PREVIEW_TASK_TIMEOUT_S)
            deadline = monotonic() + PREVIEW_TASK_TIMEOUT_S
            while monotonic() < deadline:
                task = await self.sb.get_task(run.sandbox_id, task_id)
                if task.get("status") != "running":
                    break
                await self._poll_wait(run)
            info = await self.sb.get_sandbox(run.sandbox_id)
            run.preview_url = (info.get("preview") or {}).get("url") or None
            await dbmod.save_run(self.db, run)
        except Exception:  # noqa: BLE001 — preview is auxiliary
            pass

    async def _notify_awaiting(self, run: Run) -> None:
        try:
            msg_id = await self.tg.notify_awaiting_approval(run)
            if msg_id:
                run.tg_approval_message_id = msg_id
                await dbmod.save_run(self.db, run)
                # Videos ride the pause only when the approval message made
                # it out; otherwise _report_success delivers them at the end
                # (its guard fires on tg_approval_message_id is None) — never
                # both.
                await self._send_e2e_videos(run)
        except Exception:  # noqa: BLE001
            pass

    async def expire_preview(self, run: Run) -> None:
        """TTL sweep: tear down the paused run's sandbox; the run stays paused."""
        try:
            await self.sb.delete_app(run.app_id)
        except Exception:  # noqa: BLE001 — retried on the next sweep
            return
        run.app_id = None
        run.sandbox_id = None
        run.preview_url = None
        run.sandbox_expires_at = None
        await dbmod.save_run(self.db, run)
        await dbmod.add_event(self.db, run.id, AWAITING_APPROVAL, AWAITING_APPROVAL,
                              "preview expired — sandbox deleted")
        await self._refresh_card(run)

    async def process(self, run: Run) -> None:
        if run.kind == "planning":
            return await self.process_planning(run)
        try:
            if run.state == QUEUED:
                # A run recovered while still QUEUED already has its topic and
                # card — reuse them instead of minting orphaned duplicates.
                if run.tg_thread_id is None:
                    run.tg_thread_id = await self.tg.start_run_thread(run)
                if run.tg_card_message_id is None:
                    events = await dbmod.events_for_run(self.db, run.id)
                    run.tg_card_message_id = await self.tg.send_card(run, events)
                await dbmod.save_run(self.db, run)
                await self._swap_labels_start(run)
                await transition(self.db, run, PREPARING)
                await self._refresh_card(run)
            if run.state == PREPARING:
                await self._prepare(run)
                await transition(self.db, run, EXECUTING)
                await self._refresh_card(run)
            if run.state == EXECUTING:
                try:
                    await self._execute(run)
                except ExecutionTimeout:
                    await self.sb.cancel_task(run.sandbox_id, run.task_id)
                    await self._publish_partial(run)
                    raise RunFailure(
                        EXECUTING,
                        f"timed out after {run.timeout_minutes} minutes of agent work",
                    ) from None
                except RunFailure:
                    await self._publish_partial(run)
                    raise
                await transition(
                    self.db, run,
                    REVIEWING if run.review_enabled
                    else E2E_TESTING if run.e2e_enabled else STAGING)
                await self._refresh_card(run)
            if run.state == REVIEWING:
                await self._review(run)
                await transition(self.db, run,
                                 E2E_TESTING if run.e2e_enabled else STAGING)
                await self._refresh_card(run)
            if run.state == E2E_TESTING:
                await self._e2e(run)
                await transition(self.db, run, STAGING)
                await self._refresh_card(run)
            if run.state == STAGING:
                staged = await self._stage(run)
                if staged and run.approval_mode == "always":
                    await self._start_preview(run)
                    # Nothing polls during the pause, so the idle reaper would
                    # stop the sandbox — and kill the preview link — long before
                    # our own TTL expires. Hold it for the whole window; the
                    # worker's reaper is what actually ends the pause.
                    await self.sb.keepalive(run.sandbox_id,
                                            self.settings.preview_ttl_minutes)
                    run.sandbox_expires_at = (
                        datetime.now(timezone.utc)
                        + timedelta(minutes=self.settings.preview_ttl_minutes)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    await transition(self.db, run, AWAITING_APPROVAL)
                    await self._refresh_card(run)
                    await self._notify_awaiting(run)
                    return  # release the worker slot; approve/revise/discard re-enqueue
                await transition(self.db, run, PUBLISHING)
                await self._refresh_card(run)
            if run.state == PUBLISHING:
                await self._publish_ff(run)
                await transition(self.db, run, REPORTING)
                await self._refresh_card(run)
            if run.state == REPORTING:
                await self._report_success(run)
                await transition(self.db, run, DONE)
                await self._refresh_card(run)
                await self.tg.finish_run_thread(run)
                await self.sb.delete_app(run.app_id)
        except RunFailure as f:
            await self.fail(run, f.stage, str(f))
        except Exception as e:  # noqa: BLE001 — every failure must still be reported
            await self.fail(run, run.state, f"internal error: {e!r}")

    async def process_planning(self, run: Run) -> None:
        try:
            if run.state == QUEUED:
                if run.tg_thread_id is None:
                    run.tg_thread_id = await self.tg.start_run_thread(run)
                    await it.set_topic(self.db, run.repo, run.issue_number,
                                       run.tg_thread_id)
                if run.tg_card_message_id is None:
                    events = await dbmod.events_for_run(self.db, run.id)
                    run.tg_card_message_id = await self.tg.send_card(run, events)
                await dbmod.save_run(self.db, run)
                await transition(self.db, run, PREPARING)
                await self._refresh_card(run)
            if run.state == PREPARING:
                await self._prepare_planning(run)
                await transition(self.db, run, PLANNING)
                await self._refresh_card(run)
            if run.state == PLANNING:
                result = await self._planning(run)
                run.summary = (result.summary or
                               "\n".join(f"- {q}" for q in result.questions))
                await dbmod.save_run(self.db, run)
                if result.outcome == "questions":
                    await transition(self.db, run, REPORTING,
                                     detail="questions for the issue author")
                else:
                    await transition(self.db, run, PUBLISHING)
                await self._refresh_card(run)
            if run.state == PUBLISHING:
                await self._publish_plan(run)
                await transition(self.db, run, REPORTING)
                await self._refresh_card(run)
            if run.state == REPORTING:
                await self._report_planning(run)
                await transition(self.db, run, DONE)
                await self._refresh_card(run)
                await self.sb.delete_app(run.app_id)
        except RunFailure as f:
            await self.fail(run, f.stage, str(f))
        except Exception as e:  # noqa: BLE001 — every failure must still be reported
            await self.fail(run, run.state, f"internal error: {e!r}")

    async def _prepare_planning(self, run: Run) -> None:
        raw = await self.gh.get_file(run.repo, run.head_branch, ".loop.yml")
        if raw is None:
            raise RunFailure(PREPARING, "no .loop.yml in the repository")
        try:
            cfg = parse_loop_config(raw)
        except LoopConfigError as e:
            raise RunFailure(PREPARING, f".loop.yml is invalid: {e}") from e
        run.spec_path, run.plan_path = plan_paths(cfg.specs_dir, run.issue_number)
        run.timeout_minutes = cfg.timeout_minutes or self.settings.default_timeout_minutes
        repo_secrets = load_repo_secrets(self.settings.secrets_dir, run.repo)
        missing = [k for k in cfg.required_env if k not in repo_secrets]
        if missing:
            raise RunFailure(PREPARING,
                             "missing project secrets: " + ", ".join(missing))
        run.prompt = build_planner_prompt(run.issue_number, run.spec_path,
                                          run.plan_path, cfg.setup,
                                          repo_secrets)
        for old_app in await dbmod.previous_app_ids_for_issue(
                self.db, run.repo, run.issue_number, run.id):
            await self.sb.delete_app(old_app)
        run.app_id = await self.sb.create_app(
            name=planning_app_name(run),
            repo_url=f"https://github.com/{run.repo}.git",
            branch=run.head_branch,
            credential_id=self.settings.git_credential_id,
            preset=cfg.sandbox_preset,
        )
        await dbmod.save_run(self.db, run)
        for key, value in repo_secrets.items():
            await self.sb.set_app_secret(run.app_id, key, value)
        run.sandbox_id = await self.sb.create_sandbox(run.app_id)
        await dbmod.save_run(self.db, run)
        await self._write_secrets(run, repo_secrets)

    async def _planning(self, run: Run) -> PlannerResult:
        """Planner produces spec+plan; the Implementor Advisor gates them."""
        deadline = monotonic() + run.timeout_minutes * 60
        task_timeout_s = min(run.timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        prompt = run.prompt
        iteration = 0
        while True:
            try:
                # The planner keeps its own session across revise rounds so the
                # repo study and the advisor's remarks stay in context.
                task, deadline = await self._run_sandbox_task(
                    run, prompt, task_timeout_s, deadline,
                    model=self.settings.planner_model or None,
                    continue_session=iteration > 0)
                result = parse_planner_output(task.get("agent_message_final")
                                              or task.get("agent_message") or "")
            except ReviewDeadline:
                raise RunFailure(PLANNING, "planning timed out") from None
            except (ReviewTaskError, PlanningError) as e:
                raise RunFailure(PLANNING, f"planner failed: {e}") from e
            if result.outcome == "questions":
                return result
            try:
                # The advisor always gets a fresh session: it must judge the
                # written documents, not inherit the planner's reasoning.
                task, deadline = await self._run_sandbox_task(
                    run, build_advisor_prompt(run.spec_path, run.plan_path),
                    task_timeout_s, deadline, model=self.settings.advisor_model)
                verdict = parse_advisor_verdict(task.get("agent_message_final")
                                                or task.get("agent_message") or "")
            except ReviewDeadline:
                raise RunFailure(PLANNING, "planning timed out") from None
            except (ReviewTaskError, PlanningError) as e:
                raise RunFailure(PLANNING, f"advisor failed: {e}") from e
            await dbmod.add_event(self.db, run.id, PLANNING, PLANNING,
                                  f"advisor verdict: {verdict.verdict}")
            if verdict.verdict == "approved":
                return result
            if iteration >= self.settings.plan_max_iterations:
                raise RunFailure(
                    PLANNING,
                    "the advisor did not approve the plan after "
                    f"{iteration + 1} iteration(s): {verdict.summary} "
                    f"(issues: {'; '.join(verdict.issues)})")
            iteration += 1
            prompt = build_planner_revise_prompt(verdict)

    async def sync_branch_with_base(self, run: Run, base_ref: str) -> list[str]:
        """Resolve merge conflicts between the PR branch and its base.

        A fresh app+sandbox on the PR branch (the run's own sandbox is gone by
        DONE); the agent fetches the base with a temporary GIT_SYNC_TOKEN app
        secret (write-only, dies with the app), merges and resolves; the merge
        commit travels through the usual push path — a NEW temp branch, then a
        fast-forward of the PR branch (valid: the merge commit's first parent
        is the current PR head). Returns the resolved paths; raises SyncError.
        """
        sync_branch = f"loop/run-{run.id}-sync"
        try:
            await self.gh.delete_branch(run.repo, sync_branch)  # stale leftover
        except Exception:  # noqa: BLE001
            pass
        app_id = await self.sb.create_app(
            name=sync_app_name(run),
            repo_url=f"https://github.com/{run.repo}.git",
            branch=run.head_branch,
            credential_id=self.settings.git_credential_id,
        )
        run.app_id, run.sandbox_id = app_id, None
        await dbmod.save_run(self.db, run)
        try:
            run.sandbox_id = await self.sb.create_sandbox(app_id)
            await dbmod.save_run(self.db, run)
            # A file, not app config: config never reaches a sandbox, and a
            # *_TOKEN env var would be scrubbed out of the agent's environment
            # anyway (see SandboxdClient.put_file).
            await self._write_secrets(
                run, {"GIT_SYNC_TOKEN": self.settings.github_token})
            try:
                task, _ = await self._run_sandbox_task(
                    run, build_sync_prompt(run.repo, base_ref),
                    SYNC_TASK_TIMEOUT_S, monotonic() + SYNC_TASK_TIMEOUT_S)
            except (ReviewDeadline, ReviewTaskError) as e:
                raise SyncError(f"resolution agent failed: {e}") from e
            verdict = find_json_object(task.get("agent_message_final")
                                       or task.get("agent_message") or "",
                                       prefer_key="resolved")
            if not verdict or not verdict.get("resolved"):
                notes = (verdict or {}).get("notes") or "no verdict"
                raise SyncError(f"the agent could not resolve the conflict: {notes}")
            await self.sb.sanitize_git_config(run.sandbox_id)
            push = await self.sb.git_push(app_id, sync_branch)
            if not push.get("pushed"):
                raise SyncError(f"push rejected by sandboxd: {push.get('reason')}")
            sha = await self.gh.branch_sha(run.repo, sync_branch)
            try:
                await self.gh.fast_forward(run.repo, run.head_branch, sha)
            except FastForwardError as e:
                raise SyncError(
                    f"the PR branch moved during resolution; the merge is "
                    f"preserved in branch {sync_branch}") from e
            await self.gh.delete_branch(run.repo, sync_branch)
            files = [f for f in verdict.get("files") or [] if isinstance(f, str)]
            await dbmod.add_event(self.db, run.id, DONE, DONE,
                                  "merge conflicts resolved: "
                                  + (", ".join(files) or "(files not named)"))
            return files
        finally:
            try:
                await self.sb.delete_app(app_id)
            except Exception:  # noqa: BLE001
                pass
            run.app_id = None
            run.sandbox_id = None
            await dbmod.save_run(self.db, run)

    async def _refresh_card(self, run: Run) -> None:
        """Best-effort card repaint; notification failures never fail the run."""
        try:
            events = await dbmod.events_for_run(self.db, run.id)
            await self.tg.update_card(run, events)
        except Exception:  # noqa: BLE001
            pass

    # A run has exactly one outcome, so a PR wears exactly one of these. Both
    # halves of that invariant were broken live: PR #16 carried loop:run next
    # to the previous pass's loop:done, and PR #13 ended up with loop:failed
    # and loop:done at once after a failed run was resumed and finished.
    _VERDICT_LABELS = ("loop:done", "loop:needs-review", "loop:failed")

    async def _set_verdict_label(self, repo: str, number: int,
                                 label: str | None) -> None:
        """Make `label` the only loop verdict on the PR/issue (None = clear)."""
        for stale in self._VERDICT_LABELS:
            if stale == label:
                continue
            try:
                await self.gh.remove_label(repo, number, stale)
            except Exception:  # noqa: BLE001 — a label that isn't there is fine
                pass
        if label:
            await self.gh.add_labels(repo, number, [label])

    async def _swap_labels_start(self, run: Run) -> None:
        await self.gh.ensure_labels(run.repo)
        await self.gh.remove_label(run.repo, run.pr_number, "loop:run")
        await self._set_verdict_label(run.repo, run.pr_number, None)
        await self.gh.add_labels(run.repo, run.pr_number, ["loop:running"])

    async def _report_success(self, run: Run) -> None:
        escalated = (run.review_status == "escalated"
                     or run.e2e_status == "escalated")
        await self.gh.remove_label(run.repo, run.pr_number, "loop:running")
        # Replaces any earlier verdict: a run that failed, was resumed and then
        # finished must not leave loop:failed sitting next to loop:done.
        await self._set_verdict_label(
            run.repo, run.pr_number,
            "loop:needs-review" if escalated else "loop:done")
        await self.gh.create_comment(
            run.repo, run.pr_number,
            f"✅ Loop run #{run.id} finished.\n\n{run.summary or ''}")
        if run.review_status:
            report = json.loads(run.review_json or "{}")
            await self.gh.create_comment(
                run.repo, run.pr_number,
                format_review_comment(run.review_status, run.review_iteration, report))
        if run.e2e_status:
            e2e_report = json.loads(run.e2e_json or "{}")
            await self.gh.create_comment(
                run.repo, run.pr_number,
                format_e2e_comment(run.e2e_status, run.e2e_iteration, e2e_report))
        await self.tg.notify_done(run)
        if run.review_status == "escalated":
            remaining = len(json.loads(run.review_json or "{}").get("remaining", []))
            await self.tg.notify_review_escalation(run, remaining)
        if run.e2e_status == "escalated":
            failing = sum(1 for t in json.loads(run.e2e_json or "{}").get("tests", [])
                          if t.get("status") == "failed")
            await self.tg.notify_e2e_escalation(run, failing)
        # Paused runs already got their videos with the approval request.
        if run.tg_approval_message_id is None:
            await self._send_e2e_videos(run)

    async def _send_e2e_videos(self, run: Run) -> None:
        if run.e2e_status not in ("passed", "escalated"):
            return
        report = json.loads(run.e2e_json or "{}")
        paths = select_video_paths(run.e2e_status, report)
        if not paths:
            return
        try:
            entries = await self.sb.list_files(run.sandbox_id, E2E_DIR)
            sizes = {e["path"]: e.get("size", 0) for e in entries
                     if e.get("type") == "file"}
            wanted = [p for p in paths
                      if p in sizes and sizes[p] <= MAX_VIDEO_BYTES]
            videos: dict[str, bytes] = {}
            small = [p for p in wanted if sizes[p] <= 2 * 1024 * 1024]
            large = [p for p in wanted if sizes[p] > 2 * 1024 * 1024]
            for p in small:
                data = await self.sb.read_file(run.sandbox_id, p)
                if data:
                    videos[p] = data
            if large:
                videos.update(extract_from_zip(
                    await self.sb.export_zip(run.sandbox_id), large))
            for p in wanted:
                if p in videos:
                    name = PurePosixPath(p).name
                    await self.tg.send_video(
                        videos[p], name, f"🎬 {run_title(run)} — {name}",
                        thread_id=run.tg_thread_id)
            skipped = len(paths) - len(videos)
            if skipped:
                await self.tg.send(
                    f"⚠️ {run_title(run)}: {skipped} e2e video(s) skipped "
                    "(missing or over 45 MB).", thread_id=run.tg_thread_id)
        except Exception:  # noqa: BLE001 — video delivery must never fail the run
            await self.tg.send(f"⚠️ {run_title(run)}: e2e video could not be delivered.",
                               thread_id=run.tg_thread_id)

    async def fail(self, run: Run, stage: str, message: str) -> None:
        fresh = await dbmod.get_run(self.db, run.id)
        if fresh is not None and fresh.state == CANCELLED:
            return  # a concurrent cancel/discard won — not a failure
        run.error = f"[{stage}] {message}"
        try:
            await transition(self.db, run, FAILED, detail=run.error)
        except InvalidTransition:
            run.state = FAILED
            await dbmod.save_run(self.db, run)
        # Planning runs live on the issue, not on a PR (which may not exist yet).
        number = run.pr_number if run.kind == "pr" else run.issue_number
        actions = [
            lambda: self._set_verdict_label(run.repo, number, "loop:failed"),
            lambda: self.gh.create_comment(
                run.repo, number, f"❌ Loop run #{run.id} failed: {run.error}"),
            lambda: self.tg.notify_failed(run),
            lambda: self._refresh_card(run),
            lambda: self.tg.finish_run_thread(run),
        ]
        if run.kind == "pr":  # planning runs never set loop:running
            actions.insert(
                0, lambda: self.gh.remove_label(run.repo, number, "loop:running"))
        for action in actions:
            try:
                await action()
            except Exception:  # noqa: BLE001 — reporting must not fail as a whole
                pass
