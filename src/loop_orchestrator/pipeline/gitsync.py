"""Resolving a PR branch that conflicts with its base, in a throwaway sandbox.

Reached from `actions.py` when a merge button hits a conflict, long after the
Run's own sandbox is gone — hence a fresh app on the PR branch, a temporary
`GIT_SYNC_TOKEN` that dies with it, and the same push path every other stage
uses (a new temp branch, then a fast-forward).
"""

from .. import db as dbmod
from ..clients.github import FastForwardError
from ..jsonextract import find_json_object
from ..models import DONE, Run
from ..secrets import SOURCE_LINE
from . import clock
from .errors import ReviewDeadline, ReviewTaskError, SyncError

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
        "compose — never drop either side.\n"
        "Generated files (lockfiles, compiled assets) are never merged by hand: "
        "take either side, then re-derive them with the tool that owns them. "
        "Scratch files under `.loop/` take the base side.\n\n"
        "Then check what merged WITHOUT a conflict — that is where this goes "
        "wrong. Two branches that each added 'the next' sequentially numbered "
        "artefact (a database migration, a numbered decision record, an ordered "
        "fixture) produce two different filenames, so git reports a clean merge "
        "while the result is broken: a forked migration graph, two documents "
        "claiming the same number. List what each side added under such "
        "directories and renumber YOUR side to follow the base's, re-pointing "
        "any parent reference and any link that named the old identifier. If "
        "the project has a migration tool, run its heads/status check and "
        "confirm exactly one head.\n"
        "If the repository documents its own checks (in CLAUDE.md or its "
        "skills), run them on the merged tree before committing — a merge that "
        "parses is not a merge that works.\n\n"
        "Conclude the merge with `git add -A && git commit --no-edit`. Do not "
        "push. Do not switch branches. Do not amend or rebase existing commits. "
        "Do not open or update a pull request and do not run `gh` — publishing "
        "is handled by an external system. A repository skill may instruct you "
        "to push and open a PR once the branch is in order; that part does not "
        "apply here, the merge commit is all that is needed.\n\n"
        "Your FINAL message must be a single JSON object and nothing else:\n"
        '{"resolved": true|false, "files": ["path", ...], "notes": "one line"}\n'
        'Set "resolved": false only if the conflict cannot be resolved '
        "faithfully; explain why in notes."
    )


class SyncMixin:
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
                # The resolver's sandbox was created a few lines above, so
                # fresh is the only possible answer; stated anyway so a future
                # reader does not have to re-derive it from the app lifecycle.
                task, _ = await self._run_sandbox_task(
                    run, build_sync_prompt(run.repo, base_ref),
                    SYNC_TASK_TIMEOUT_S, clock.monotonic() + SYNC_TASK_TIMEOUT_S,
                    continue_session=False, trace_stage="git-sync")
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
