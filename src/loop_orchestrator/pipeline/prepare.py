"""Everything a Run needs before an agent may touch it.

Reads `.loop.yml`, snapshots its knobs onto the Run (a config edited mid-Run
must not change the rules the Run started under), creates the app and the
sandbox, and places the secrets and the upstream context inside it. Both Run
kinds prepare here — `_prepare` for a PR Run, `_prepare_planning` for a
planning one — because the two differ only in which document they aim the
agent at.
"""

import json

from .. import db as dbmod
from .. import issue_tasks as it
from ..contracts import (
    CONTEXT_DIR,
    collect_upstreams,
    fetch_context_files,
    render_context_readme,
)
from ..loopconfig import LoopConfigError, find_spec_plan_pair, parse_loop_config
from ..models import PREPARING, Run
from ..planning import build_planner_prompt, plan_paths
from ..review import (
    # One definition, three stages: the executor, the reviewer and the e2e agent
    # all pay the same bill, and two verbatim copies of the rule would drift.
    WORKING_EFFICIENTLY,
)
from ..secrets import (
    SECRETS_FILE,
    SECRETS_GITIGNORE,
    load_repo_secrets,
    render_env_file,
    source_hint,
)
from .errors import RunFailure


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
        "Finish with a short summary: what was done, what was verified, what failed.\n\n"
        + WORKING_EFFICIENTLY
    )


class PrepareMixin:
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
        # Only a Run tied to an issue can hand anything to a dependent task;
        # whether the issue actually blocks anyone is decided at stage time,
        # because a dependency may be added hours after prepare.
        run.contract_enabled = run.issue_number is not None

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
        await self._write_context(run)

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
        # Snapshot the repository's planning knobs onto the Run: the config is
        # read once, and a `.loop.yml` edited mid-Run must not change the rules
        # the Run started under.
        run.planner_model = cfg.planner_model
        run.advisor_enabled = cfg.advisor_enabled
        run.advisor_model = cfg.advisor_model
        run.plan_max_iterations = cfg.plan_max_iterations
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
        await self._write_context(run)

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

    async def _write_context(self, run: Run) -> None:
        """Place the upstream sources next to the secrets, under `.loop/`.

        Best-effort: the digest in `.loop/task.md` is already committed, so a
        failure here degrades the context rather than losing it. `.loop/.gitignore`
        (written with the secrets) is what keeps these copies out of the commit.
        """
        if run.issue_number is None:
            return
        task = await it.get_task(self.db, run.repo, run.issue_number)
        if task is None or not task.depends_on:
            return
        try:
            upstreams = await collect_upstreams(self.db, self.gh, task)
            files, dropped = await fetch_context_files(self.gh, upstreams)
            if not files:
                return
            await self.sb.put_file(run.sandbox_id, SECRETS_GITIGNORE, "*\n")
            for path, text in files.items():
                await self.sb.put_file(run.sandbox_id, path, text)
            await self.sb.put_file(run.sandbox_id, f"{CONTEXT_DIR}/README.md",
                                   render_context_readme(upstreams, dropped))
        except Exception as e:  # noqa: BLE001 — the snapshot still carries the digest
            await dbmod.add_event(self.db, run.id, run.state, run.state,
                                  f"upstream context not delivered: {e}")
