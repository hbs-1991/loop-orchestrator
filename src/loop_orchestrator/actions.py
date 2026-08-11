"""Single entry point for human actions on runs.

Telegram buttons today and the dashboard (phase 4c) call these methods;
neither touches the pipeline or worker directly. Every action validates
"state x action" atomically, records the actor in run_events, then applies
its effect. Invalid requests raise ActionError with a user-facing message.
"""
import asyncio
from typing import NamedTuple

from . import db as dbmod
from . import issue_tasks as it
from .clients.github import GitHubError
from .models import (
    AWAITING_APPROVAL,
    CANCELABLE,
    CANCELLED,
    DONE,
    EXECUTING,
    FAILED,
    PUBLISHING,
    Run,
)
from .pipeline import MAX_TASK_TIMEOUT_S, SyncError
from .review import build_revise_prompt
from .state_machine import transition


class ActionError(Exception):
    """The action is not applicable; str(e) is safe to show to the user."""


class Gate(NamedTuple):
    """What the merge gate sees for one PR.

    `state` is one of "clean" | "behind" | "conflicts" | "checks_failed" |
    "checks_pending". `red` carries the failed check names when the state is
    checks_failed, and the still-missing required ones when checks_pending.
    `done`/`total` count finished checks against the ones that hold the merge —
    they exist for the button, not for the decision.
    """
    state: str
    base: str
    red: list[str]
    done: int = 0
    total: int = 0


class Actions:
    def __init__(self, db, settings, gh, sb, tg, worker, pipeline):
        self.db = db
        self.settings = settings
        self.gh = gh
        self.sb = sb
        self.tg = tg
        self.worker = worker
        self.pipeline = pipeline
        # One lock serialises the check-then-act of every action: two clicks
        # on the same button can never both pass validation.
        self._lock = asyncio.Lock()
        # In-flight conflict resolutions, keyed by run id: strong refs so the
        # tasks are not GC'd, and a guard against starting a second one.
        self._syncs: dict[int, asyncio.Task] = {}

    async def _load(self, run_id: int, *states: str) -> Run:
        run = await dbmod.get_run(self.db, run_id)
        if run is None:
            raise ActionError(f"run #{run_id} not found")
        if run.state not in states:
            raise ActionError(f"run #{run_id} is already {run.state}")
        return run

    async def approve(self, run_id: int, actor: int) -> str:
        async with self._lock:
            run = await self._load(run_id, AWAITING_APPROVAL)
            await transition(self.db, run, PUBLISHING, detail=f"approved by tg:{actor}")
        await self.pipeline._refresh_card(run)
        self.worker.enqueue(run.id)
        return f"✅ run #{run.id} approved — publishing"

    async def discard(self, run_id: int, actor: int) -> str:
        async with self._lock:
            run = await self._load(run_id, AWAITING_APPROVAL)
            await transition(self.db, run, CANCELLED, detail=f"discarded by tg:{actor}")
        note = (f"The staged code remains in branch {run.staging_branch}."
                if run.staging_branch else "")
        await self._cleanup_cancelled(run, note)
        return f"🚫 run #{run.id} discarded"

    async def revise(self, run_id: int, actor: int, feedback: str) -> str:
        async with self._lock:
            run = await self._load(run_id, AWAITING_APPROVAL)
            if not run.sandbox_id:
                raise ActionError(
                    f"run #{run.id}: the sandbox has expired — approve, discard "
                    "or restart instead")
            # Feedback on the executor's work belongs in the executor's session
            # — but sandboxd's `continue` resumes *the most recent* session and
            # offers no way to name an older one, so it only lands there when
            # nothing ran after the executor. Review and e2e each open a session
            # of their own; with either of them on, `continue` would resume the
            # reviewer or the e2e agent, which knows the tests and not the
            # implementation. Then a fresh session with a prompt that restates
            # the branch, the documents and the test command is both cheaper and
            # better informed.
            resumed = not (run.review_enabled or run.e2e_enabled)
            # The pause sleeps its sandbox (see Pipeline._sleep_pause), so wake
            # it before submitting. Idempotent on a running one, and it returns
            # only once the container is up, so the submit that follows does not
            # race the start.
            await self.sb.start_sandbox(run.sandbox_id)
            try:
                run.task_id = await self.sb.submit_task(
                    run.sandbox_id,
                    build_revise_prompt(feedback, run.test_cmd,
                                        head_branch=run.head_branch,
                                        spec_path=run.spec_path,
                                        plan_path=run.plan_path,
                                        resumed=resumed),
                    timeout_s=min(run.timeout_minutes * 60, MAX_TASK_TIMEOUT_S),
                    continue_session=resumed)
            except Exception as e:  # noqa: BLE001 — dead sandbox, network, ...
                raise ActionError(f"run #{run.id}: could not reach the sandbox "
                                  f"({e}) — approve, discard or restart") from e
            # Free the temp branch only after the submit succeeded: a failed
            # revise must leave the staged work approvable. sandboxd pushes
            # only to NEW branches, and the next staging pass re-pushes the
            # same name. If the delete itself fails, staging will fail
            # honestly on the existing branch.
            if run.staging_branch:
                try:
                    await self.gh.delete_branch(run.repo, run.staging_branch)
                except Exception:  # noqa: BLE001
                    pass
                run.staging_branch = None
            # A fresh verification cycle for the revised work.
            run.review_status = None
            run.review_iteration = 0
            run.review_json = None
            run.e2e_status = None
            run.e2e_iteration = 0
            run.e2e_json = None
            run.sandbox_expires_at = None
            await transition(self.db, run, EXECUTING, detail=f"revise by tg:{actor}")
        await self._start_revision_card(run)
        self.worker.enqueue(run.id)
        return f"✏️ run #{run.id}: feedback sent to the agent"

    async def _start_revision_card(self, run: Run) -> None:
        """Post a fresh progress card for the fix cycle (best-effort).

        The old card stays in the thread as the previous cycle's history;
        render_card restarts stage times at the new `executing` event.
        """
        try:
            events = await dbmod.events_for_run(self.db, run.id)
            run.tg_card_message_id = await self.tg.send_card(run, events)
            await dbmod.save_run(self.db, run)
        except Exception:  # noqa: BLE001 — cards must never fail an action
            pass

    async def cancel(self, run_id: int, actor: int) -> str:
        async with self._lock:
            run = await self._load(run_id, *CANCELABLE)
            await transition(self.db, run, CANCELLED, detail=f"cancelled by tg:{actor}")
        if run.task_id:
            await self.sb.cancel_task(run.sandbox_id, run.task_id)
        note = ""
        if run.app_id and await self.pipeline.rescue_to_staging(run):
            note = f"The agent's work is preserved in branch {run.staging_branch}."
        await self._cleanup_cancelled(run, note)
        return f"🚫 run #{run.id} cancelled"

    async def restart(self, run_id: int, actor: int) -> str:
        async with self._lock:
            old = await dbmod.get_run(self.db, run_id)
            if old is None:
                raise ActionError(f"run #{run_id} not found")
            if old.state not in (FAILED, CANCELLED):
                raise ActionError(
                    f"run #{run_id} is {old.state} — restart applies to failed "
                    "or cancelled runs")
            # A backlog run belongs to an issue chain, not to a PR: dedupe by
            # issue, rebuild the same kind of run and hand the task back to it.
            if old.issue_number is not None:
                existing = await dbmod.active_run_for_issue(
                    self.db, old.repo, old.issue_number)
                if existing is not None:
                    raise ActionError(
                        f"run #{existing.id} is already active for "
                        f"{old.repo} issue #{old.issue_number}")
                if old.kind == "planning":
                    new = await dbmod.create_planning_run(
                        self.db, old.repo, old.issue_number,
                        old.head_branch, old.pr_title, old.lane)
                else:
                    new = await dbmod.create_run(
                        self.db, repo=old.repo, pr_number=old.pr_number,
                        head_branch=old.head_branch, pr_title=old.pr_title)
                    new.issue_number = old.issue_number
                    new.lane = old.lane
                    new.tg_thread_id = old.tg_thread_id
                    await dbmod.save_run(self.db, new)
                await it.set_run(self.db, old.repo, old.issue_number, new.id)
                await it.set_state(self.db, old.repo, old.issue_number, it.RUNNING)
            else:
                existing = await dbmod.active_run_for_pr(self.db, old.repo,
                                                         old.pr_number)
                if existing is not None:
                    raise ActionError(
                        f"run #{existing.id} is already active for "
                        f"{old.repo}#{old.pr_number}")
                new = await dbmod.create_run(self.db, repo=old.repo,
                                             pr_number=old.pr_number,
                                             head_branch=old.head_branch,
                                             pr_title=old.pr_title)
            await dbmod.add_event(self.db, new.id, None, new.state,
                                  f"restarted from run #{old.id} by tg:{actor}")
        if old.issue_number is not None:
            try:
                await self.gh.remove_label(old.repo, old.issue_number, "loop:failed")
            except Exception:  # noqa: BLE001 — label cleanup is best-effort
                pass
        self.worker.enqueue(new.id)
        return f"🔁 restarted as run #{new.id}"

    async def merge(self, run_id: int, actor: int) -> str:
        return await self._merge(run_id, actor, promote=False)

    async def merge_deploy(self, run_id: int, actor: int) -> str:
        return await self._merge(run_id, actor, promote=True)

    async def update_branch(self, run_id: int, actor: int) -> str:
        """Merge the base into the PR branch — the `⤴️ Update branch` button.

        A separate action rather than a press of Merge that happens to find the
        branch stale: the button says one thing, so it must not merge if the
        gate turned clean in between.
        """
        async with self._lock:
            run = await self._load(run_id, DONE)
            if run.merged_at:
                raise ActionError(f"run #{run.id}: the PR is already merged")
            g = await self._merge_readiness(run)
            if g.state != "behind":
                raise ActionError(
                    f"run #{run.id}: the branch is not behind `{g.base}` "
                    "any more — nothing to update")
            await self.gh.update_pr_branch(run.repo, run.pr_number)
            await dbmod.add_event(self.db, run.id, DONE, DONE,
                                  f"PR branch updated from {g.base} by tg:{actor}")
            return (f"⤴️ run #{run.id}: the PR branch was behind `{g.base}` and "
                    "has been updated; the buttons will follow the re-run")

    # A check run with one of these conclusions means CI did not vouch for
    # the commit: failure and timeout obviously; cancelled because a killed
    # run verified nothing; action_required because it is blocked on a human.
    _RED_CONCLUSIONS = ("failure", "timed_out", "cancelled", "action_required")

    async def gate(self, run: Run) -> Gate:
        """Public read of the merge gate — the reaper paints buttons with it."""
        return await self._merge_readiness(run)

    async def _merge_readiness(self, run: Run) -> Gate:
        """What the gate sees; best-effort, defaults to clean.

        GitHub computes mergeability lazily; a few short polls ride out the
        `mergeable: null` window. Any API trouble falls back to "clean" so
        the merge attempt itself stays the source of truth. The checks gate
        exists because work repos have no branch protection: without it a
        red PR merges silently (happened live with PR #13's broken uv.lock).
        """
        try:
            for _ in range(5):
                pr = await self.gh.get_pr(run.repo, run.pr_number)
                if pr.get("mergeable") is not None:
                    break
                await asyncio.sleep(2)
            base = (pr.get("base") or {}).get("ref") or "main"
            if pr.get("mergeable") is False:
                return Gate("conflicts", base, [])
            if pr.get("mergeable_state") == "behind":
                return Gate("behind", base, [])
            # Only a strict base reports "behind" above, and the work repos
            # have no such rule — so ask `compare` directly. A green check run
            # on a stale branch measures a tree that will not exist after the
            # merge: the classic case is two branches that each added "the
            # next" numbered migration, which merge without a conflict and
            # fork the graph in main. Updating first puts both of them in one
            # tree while the PR can still be the thing that goes red.
            head_ref = (pr.get("head") or {}).get("ref")
            if head_ref and await self.gh.behind_by(run.repo, base, head_ref):
                return Gate("behind", base, [])
            sha = (pr.get("head") or {}).get("sha")
            if not sha:
                return Gate("clean", base, [])
            checks = await self.gh.list_check_runs(run.repo, sha)
            required = await self.gh.required_checks(run.repo, base)
            finished = {c.get("name") for c in checks
                        if c.get("status") == "completed"}
            # Progress is counted against the ruleset when there is one, so the
            # button reads "2/3" of the checks that actually hold the merge
            # rather than of whatever happens to have started.
            total = len(required) or len(checks)
            done = (sum(1 for n in required if n in finished) if required
                    else len(finished))
            red = [c.get("name") or "?" for c in checks
                   if c.get("conclusion") in self._RED_CONCLUSIONS]
            if red:
                return Gate("checks_failed", base, red, done, total)
            if any(c.get("status") != "completed" for c in checks):
                return Gate("checks_pending", base, [], done, total)
            # An empty list means "no checks yet", not "no checks at all".
            # Right after a fast-forward — the conflict resolver's last
            # step — GitHub has not created the new head's runs, and
            # merging into that window is refused by the ruleset anyway
            # ("Required status check ci is queued", seen on run #40).
            present = {c.get("name") for c in checks}
            missing = [n for n in required if n not in present]
            if missing:
                return Gate("checks_pending", base, missing, done, total)
            return Gate("clean", base, [], done, total)
        except Exception:  # noqa: BLE001
            return Gate("clean", "main", [])

    async def _merge(self, run_id: int, actor: int, promote: bool,
                     auto_sync: bool = True) -> str:
        label = self.settings.promote_label
        async with self._lock:
            run = await self._load(run_id, DONE)
            if run.merged_at:
                raise ActionError(f"run #{run.id}: the PR is already merged")
            g = await self._merge_readiness(run)
            readiness, base, red = g.state, g.base, g.red
            if readiness == "checks_failed":
                raise ActionError(
                    f"run #{run.id}: CI is red on the PR head "
                    f"({', '.join(red)}) — fix or re-run the checks, then "
                    "press the button again")
            if readiness == "checks_pending":
                names = f" ({', '.join(red)})" if red else ""
                raise ActionError(
                    f"run #{run.id}: checks are still running{names} — press "
                    "the button again when they finish")
            if readiness == "conflicts":
                if not auto_sync:
                    raise ActionError(
                        f"run #{run.id}: the PR still conflicts with {base} "
                        "after resolution — resolve manually")
                self._start_sync(run, actor, promote, base)
                return (f"⚠️ run #{run.id}: the PR conflicts with `{base}` — "
                        "started a conflict-resolution agent; the merge will "
                        "be retried automatically when it finishes")
            if readiness == "behind":
                # Update the branch and let the re-run checks gate the next
                # press — auto-merging here would race them. The extra press
                # is only spent on a branch that really is stale: at
                # behind_by == 0 nothing is synced, because an empty merge
                # commit costs a full CI run of its own.
                await self.gh.update_pr_branch(run.repo, run.pr_number)
                await dbmod.add_event(self.db, run.id, DONE, DONE,
                                      f"PR branch updated from {base} by tg:{actor}")
                return (f"⤴️ run #{run.id}: the PR branch was behind `{base}` "
                        "and has been updated; checks are re-running — press "
                        "the button again when they are green")
            if promote:
                # The promote workflow reads labels off the merged PR, so the
                # label must be in place before the merge event fires.
                try:
                    await self.gh.add_labels(run.repo, run.pr_number, [label])
                except Exception as e:  # noqa: BLE001
                    raise ActionError(
                        f"could not label {run.repo}#{run.pr_number} with "
                        f"{label} ({e}) — nothing was merged; plain Merge "
                        "still works") from e
            title = (f"{run.pr_title} (#{run.pr_number})" if run.pr_title else None)
            try:
                await self.gh.merge_pr(run.repo, run.pr_number, commit_title=title)
            except GitHubError as e:
                note = ""
                if promote:
                    # A leftover label would turn a later plain Merge into a
                    # silent deploy — take it back off.
                    try:
                        await self.gh.remove_label(run.repo, run.pr_number, label)
                    except Exception:  # noqa: BLE001
                        note = (f" (warning: {label} may still be on the PR — "
                                "remove it before merging by hand)")
                raise ActionError(f"merge rejected by GitHub: {e}{note}") from e
            run.merged_at = dbmod.utcnow()
            await dbmod.save_run(self.db, run)
            detail = f"PR merged + {label} by tg:{actor}" if promote else \
                f"PR merged by tg:{actor}"
            await dbmod.add_event(self.db, run.id, DONE, DONE, detail)
        # The PR is merged; nothing below may masquerade as a merge failure.
        steps = [lambda: self.gh.delete_branch(run.repo, run.head_branch)]
        if run.staging_branch:
            steps.append(lambda: self.gh.delete_branch(run.repo, run.staging_branch))
        steps.append(lambda: self.tg.finish_run_thread(run))  # rename to 🔀, close
        for step in steps:
            try:
                await step()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        tail = f", {label} set — staging deploy will follow" if promote else ""
        return f"🔀 {run.repo}#{run.pr_number} merged (squash){tail}"

    def _start_sync(self, run: Run, actor: int, promote: bool, base: str) -> None:
        if run.id in self._syncs:
            raise ActionError(
                f"run #{run.id}: conflict resolution is already running")
        task = asyncio.create_task(
            self._sync_and_merge(run.id, actor, promote, base))
        self._syncs[run.id] = task
        task.add_done_callback(lambda t: self._syncs.pop(run.id, None))

    async def _sync_and_merge(self, run_id: int, actor: int, promote: bool,
                              base: str) -> None:
        """Background: resolve the conflict, then retry the original merge.

        Runs outside self._lock — resolution takes minutes and must not block
        other actions. Every outcome is reported to the run's thread.
        """
        run = await dbmod.get_run(self.db, run_id)
        try:
            files = await self.pipeline.sync_branch_with_base(run, base)
            try:
                outcome = await self._merge(run_id, actor, promote,
                                            auto_sync=False)
            except ActionError as e:
                outcome = f"⚠️ {e}"
            listed = ", ".join(f"`{f}`" for f in files) or "see the merge commit"
            text = (f"🔧 run #{run_id}: merge conflicts resolved ({listed}). "
                    f"{outcome}")
        except SyncError as e:
            text = f"⚠️ run #{run_id}: conflict resolution failed: {e}"
        except Exception as e:  # noqa: BLE001 — background task must not die silently
            text = f"⚠️ run #{run_id}: conflict resolution failed: {e!r}"
        try:
            await self.tg.send(text, thread_id=run.tg_thread_id)
        except Exception:  # noqa: BLE001 — reporting is best-effort
            pass

    async def _cleanup_cancelled(self, run: Run, note: str) -> None:
        """Post-cancel teardown; every step is best-effort, like Pipeline.fail."""
        # Planning runs have no PR (pr_number is the 0 sentinel) and never got
        # the loop:running label — the notice belongs on the issue instead.
        number = run.pr_number if run.kind == "pr" else run.issue_number
        steps = [lambda: self.sb.delete_app(run.app_id)]
        if run.kind == "pr":
            steps.append(
                lambda: self.gh.remove_label(run.repo, number, "loop:running"))
        steps += [
            lambda: self.gh.create_comment(
                run.repo, number,
                f"🚫 Loop run #{run.id} was cancelled. {note}".strip()),
            lambda: self.tg.notify_cancelled(run, note),
            lambda: self.pipeline._refresh_card(run),
            lambda: self.tg.finish_run_thread(run),
        ]
        for step in steps:
            try:
                await step()
            except Exception:  # noqa: BLE001 — teardown must not fail as a whole
                pass
        run.app_id = None
        run.sandbox_id = None
        await dbmod.save_run(self.db, run)
