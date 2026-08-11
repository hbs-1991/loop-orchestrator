"""Two-phase publication: the push to a temp branch, then the fast-forward.

A sandbox cannot push to an existing branch — push is a control-plane
operation and only into a *new* branch. So `_stage` pushes `loop/run-<id>` and
`_publish_ff` fast-forwards the PR branch through the GitHub API afterwards.
The approval pause sits between the two, which is why they are separate
methods and not one.
"""

from .. import db as dbmod
from ..clients.github import FastForwardError
from ..loopconfig import resolve_base_branch
from ..models import PUBLISHING, STAGING, Run
from .errors import RunFailure


class PublishMixin:
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
