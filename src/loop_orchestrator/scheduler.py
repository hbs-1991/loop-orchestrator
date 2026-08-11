"""Backlog scheduler: GitHub issues -> lane-aware planning runs.

bootstrap() prepares the per-issue branch the sandbox will import; the
Scheduler class owns sync/pick/launch and the background poll loop.
"""
import asyncio
import logging

from . import db as dbmod
from . import issue_tasks as it
from .contracts import collect_upstreams
from .loopconfig import planning_enabled, resolve_base_branch
from .models import CANCELLED, DONE, FAILED
from .planning import TASK_FILE, build_task_file

log = logging.getLogger(__name__)

LANE_PREFIX = "loop:lane:"

QUESTION_MARKER = "Loop planner needs more information"


def branch_for_issue(issue_number: int) -> str:
    return f"loop/issue-{issue_number}"


def lane_from_labels(labels: list) -> str | None:
    for label in labels or []:
        name = label["name"] if isinstance(label, dict) else str(label)
        if name.startswith(LANE_PREFIX):
            return name[len(LANE_PREFIX):]
    return None


async def bootstrap(gh, repo: str, issue: dict, comments: list[dict],
                    upstreams: "list | tuple" = ()) -> str:
    """Ensure the issue branch exists and holds a fresh task snapshot.

    The branch must exist BEFORE the sandbox app is created (sandboxd cannot
    change an app's branch later); the task-file commit also provides the
    diff the future PR needs.
    """
    number = issue["number"]
    branch = branch_for_issue(number)
    if await gh.get_branch_sha(repo, branch) is None:
        base = await resolve_base_branch(gh, repo)
        base_sha = await gh.branch_sha(repo, base)
        await gh.create_branch(repo, branch, base_sha)
    await gh.put_file(repo, branch, TASK_FILE,
                      build_task_file(issue, comments, upstreams),
                      f"loop: task snapshot for issue #{number}")
    return branch


def pick_candidates(backlog: list["it.IssueTask"],
                    running: list["it.IssueTask"]) -> list["it.IssueTask"]:
    """Lane policy: same lane = strict queue, different lanes = parallel,
    no lane = exclusive (runs only in an otherwise empty repo)."""
    if any(t.lane is None for t in running):
        return []
    held = {t.lane for t in running}
    picked: list[it.IssueTask] = []
    for task in backlog:
        if task.lane is None:
            if not running and not picked:
                return [task]
            continue
        if task.lane not in held:
            held.add(task.lane)
            picked.append(task)
    return picked


def _iso(ts: str) -> str:
    """SQLite 'YYYY-MM-DD HH:MM:SS' (UTC) -> GitHub `since` format."""
    return ts.replace(" ", "T") + "Z"


class Scheduler:
    def __init__(self, db, settings, gh, worker):
        self.db = db
        self.settings = settings
        self.gh = gh
        self.worker = worker
        self._lock = asyncio.Lock()
        self._poll: asyncio.Task | None = None

    async def tick(self, repo: str,
                   seed_issues: list[dict] | None = None) -> None:
        """Idempotent scheduling pass; errors are logged, never raised.

        seed_issues are ready issues known from a webhook payload — merged into
        the listing because GitHub's ?labels= index lags label writes by
        seconds, so the very issue a delivery is about may be missing from it.
        """
        async with self._lock:
            try:
                await self._sync(repo, seed_issues or [])
                await self._launch_ready(repo)
            except Exception:  # noqa: BLE001 — the scheduler must survive anything
                log.warning("scheduler tick failed for %s", repo, exc_info=True)

    async def _sync(self, repo: str, seed_issues: list[dict] = ()) -> None:
        present = {i["number"]: i for i in await self.gh.list_ready_issues(repo)}
        for issue in seed_issues:
            present.setdefault(issue["number"], issue)
        for number, issue in present.items():
            task = await it.upsert_task(self.db, repo, number,
                                        issue.get("title") or "",
                                        lane_from_labels(issue.get("labels")))
            if task.state == it.WITHDRAWN:
                await it.set_state(self.db, repo, number, it.BACKLOG)
        for task in await it.tasks_for_repo(self.db, repo):
            if (task.issue_number not in present
                    and task.state in (it.BACKLOG, it.NEEDS_INFO, it.FAILED)):
                await it.set_state(self.db, repo, task.issue_number, it.WITHDRAWN)
        for task in await it.tasks_for_repo(self.db, repo):
            if task.state == it.NEEDS_INFO:
                await self._check_answered(task)
            elif task.state == it.RUNNING:
                await self._resolve_running(task)
            elif task.state == it.BACKLOG:
                # One API call answers both questions: the launch gate (open
                # blockers only) and the handoff memory (every dependency, so
                # the link survives the blocker closing).
                deps = await self.gh.issue_dependencies(repo, task.issue_number)
                await it.set_depends_on(self.db, repo, task.issue_number, [
                    {"repo": d["repo"], "number": d["number"]} for d in deps])
                await it.set_blocked_by(
                    self.db, repo, task.issue_number,
                    [d["number"] for d in deps if d["state"] == "open"])

    async def _check_answered(self, task: "it.IssueTask") -> None:
        comments = await self.gh.list_issue_comments(
            task.repo, task.issue_number, since=_iso(task.updated_at))
        if any(QUESTION_MARKER not in (c.get("body") or "") for c in comments):
            await it.set_state(self.db, task.repo, task.issue_number, it.BACKLOG)

    async def _resolve_running(self, task: "it.IssueTask") -> None:
        run = await dbmod.get_run(self.db, task.run_id) if task.run_id else None
        if run is None:
            return
        if run.state in (FAILED, CANCELLED):
            await it.set_state(self.db, task.repo, task.issue_number, it.FAILED)
            await self.gh.add_labels(task.repo, task.issue_number, ["loop:failed"])
            if run.kind == "pr":
                # Planning-run failures already commented the issue (pipeline.fail
                # targets the issue for kind=planning); execution runs comment the
                # PR there, so mirror the outcome to the issue here.
                await self.gh.create_comment(
                    task.repo, task.issue_number,
                    f"❌ Loop run #{run.id} failed: {run.error or 'see the PR'}")
            return
        if run.state != DONE:
            return  # still working
        if run.kind == "planning":
            # A planning run hands the issue over to the PR run its plan
            # produced, and the mirror normally follows that handoff
            # (webhook._link_issue_task). When the handoff is missed the mirror
            # keeps pointing at a run that finished long ago — and since a
            # running task holds its lane, one stale row stops every other issue
            # in that lane forever (seen live on issue #10: the mirror kept the
            # planning run while the PR run had already merged). Recover from
            # the runs table rather than trusting the recorded id.
            successor = await dbmod.latest_run_for_issue(
                self.db, task.repo, task.issue_number, "pr")
            if successor is not None:
                await it.set_run(self.db, task.repo, task.issue_number, successor.id)
                return  # the next tick judges the successor
            # No PR run yet: the plan's PR is waiting for its loop:run label, so
            # the task genuinely is still in flight — unless the issue is gone.
        issue = await self.gh.get_issue(task.repo, task.issue_number)
        if issue.get("state") == "closed":  # merge closed it via "Closes #N"
            await it.set_state(self.db, task.repo, task.issue_number, it.DONE)

    async def _launch_ready(self, repo: str) -> None:
        tasks = await it.tasks_for_repo(self.db, repo)
        backlog = [t for t in tasks if t.state == it.BACKLOG and not t.blocked_by]
        running = [t for t in tasks if t.state == it.RUNNING]
        candidates = pick_candidates(backlog, running)
        # Asked only when there is something to launch: a repository whose
        # backlog is empty (the common tick) costs no extra GitHub calls.
        if candidates and not await planning_enabled(self.gh, repo):
            log.debug("planning disabled by .loop.yml for %s; %d task(s) left "
                      "in the backlog", repo, len(candidates))
            return
        for task in candidates:
            issue = await self.gh.get_issue(repo, task.issue_number)
            comments = await self.gh.list_issue_comments(repo, task.issue_number)
            upstreams = await collect_upstreams(self.db, self.gh, task)
            branch = await bootstrap(self.gh, repo, issue, comments, upstreams)
            run = await dbmod.create_planning_run(
                self.db, repo, task.issue_number, branch, task.title, task.lane)
            await it.set_run(self.db, repo, task.issue_number, run.id)
            await it.set_state(self.db, repo, task.issue_number, it.RUNNING)
            self.worker.enqueue(run.id)

    async def start(self) -> None:
        self._poll = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll is not None:
            self._poll.cancel()
            try:
                await self._poll
            except asyncio.CancelledError:
                pass
            self._poll = None

    async def _poll_loop(self) -> None:
        while True:
            try:
                repos = set(self.settings.backlog_repo_list())
                repos |= set(await it.repos_with_tasks(self.db))
                for repo in sorted(repos):
                    await self.tick(repo)
            except Exception:  # noqa: BLE001 — the poller must survive anything
                log.warning("backlog poll failed", exc_info=True)
            await asyncio.sleep(self.settings.backlog_poll_minutes * 60)
