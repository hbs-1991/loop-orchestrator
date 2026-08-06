from tests.conftest import FakeGitHub, FakeSettings

from loop_orchestrator import db as dbmod
from loop_orchestrator import issue_tasks as it
from loop_orchestrator.issue_tasks import IssueTask
from loop_orchestrator.scheduler import Scheduler, pick_candidates


def _task(n, lane, state=it.BACKLOG, blocked=()):
    return IssueTask(id=n, repo="o/r", issue_number=n, title=f"T{n}", lane=lane,
                     state=state, blocked_by=list(blocked), run_id=None,
                     topic_id=None, updated_at="2026-08-03 00:00:00")


class FakeWorker:
    def __init__(self):
        self.enqueued: list[int] = []

    def enqueue(self, run_id):
        self.enqueued.append(run_id)


def _issue(n, labels=("loop:ready",), state="open"):
    return {"number": n, "title": f"T{n}", "body": "b", "state": state,
            "labels": [{"name": l} for l in labels]}


def test_pick_different_lanes_run_in_parallel():
    picked = pick_candidates([_task(1, "auth"), _task(2, "billing")], [])
    assert [t.issue_number for t in picked] == [1, 2]


def test_pick_same_lane_is_a_strict_queue():
    picked = pick_candidates([_task(2, "auth")], [_task(1, "auth", it.RUNNING)])
    assert picked == []


def test_pick_exclusive_task_waits_for_empty_repo_and_blocks_others():
    assert pick_candidates([_task(2, None)], [_task(1, "auth", it.RUNNING)]) == []
    assert pick_candidates([_task(2, "auth")], [_task(1, None, it.RUNNING)]) == []
    picked = pick_candidates([_task(1, None), _task(2, "auth")], [])
    assert [t.issue_number for t in picked] == [1]  # exclusive runs alone


async def test_tick_launches_planning_run_for_ready_issue(db):
    gh = FakeGitHub()
    gh.branch_shas["main"] = "base"
    gh.ready_issues = [_issue(7, ("loop:ready", "loop:lane:auth"))]
    worker = FakeWorker()
    sched = Scheduler(db=db, settings=FakeSettings(), gh=gh, worker=worker)
    await sched.tick("o/r")
    task = await it.get_task(db, "o/r", 7)
    assert task.state == it.RUNNING and task.run_id is not None
    run = await dbmod.get_run(db, task.run_id)
    assert (run.kind, run.issue_number, run.lane) == ("planning", 7, "auth")
    assert run.head_branch == "loop/issue-7"
    assert worker.enqueued == [run.id]


async def test_tick_seed_launches_despite_stale_listing(db):
    # The labeled webhook fires before GitHub's ?labels= index catches up:
    # the listing is empty, but the seeded issue must still launch.
    gh = FakeGitHub()
    gh.branch_shas["main"] = "base"
    gh.ready_issues = []
    worker = FakeWorker()
    sched = Scheduler(db=db, settings=FakeSettings(), gh=gh, worker=worker)
    await sched.tick("o/r", seed_issues=[_issue(7, ("loop:ready", "loop:lane:auth"))])
    task = await it.get_task(db, "o/r", 7)
    assert task.state == it.RUNNING and task.lane == "auth"
    assert len(worker.enqueued) == 1
    # a later tick with the listing caught up must not double-launch
    gh.ready_issues = [_issue(7, ("loop:ready", "loop:lane:auth"))]
    await sched.tick("o/r")
    assert len(worker.enqueued) == 1


async def test_tick_respects_open_blockers(db):
    gh = FakeGitHub()
    gh.branch_shas["main"] = "base"
    gh.ready_issues = [_issue(7)]
    gh.blocked[7] = [3]
    worker = FakeWorker()
    sched = Scheduler(db=db, settings=FakeSettings(), gh=gh, worker=worker)
    await sched.tick("o/r")
    assert (await it.get_task(db, "o/r", 7)).state == it.BACKLOG
    assert worker.enqueued == []
    gh.blocked[7] = []          # blocker closed
    await sched.tick("o/r")
    assert (await it.get_task(db, "o/r", 7)).state == it.RUNNING


async def test_tick_withdraws_unlabeled_and_revives_relabeled(db):
    gh = FakeGitHub()
    gh.branch_shas["main"] = "base"
    gh.ready_issues = []
    await it.upsert_task(db, "o/r", 7, "T", None)
    sched = Scheduler(db=db, settings=FakeSettings(), gh=gh, worker=FakeWorker())
    await sched.tick("o/r")
    assert (await it.get_task(db, "o/r", 7)).state == it.WITHDRAWN
    gh.ready_issues = [_issue(7)]
    await sched.tick("o/r")
    assert (await it.get_task(db, "o/r", 7)).state == it.RUNNING


async def test_tick_marks_failed_run_and_labels_issue(db):
    gh = FakeGitHub()
    gh.ready_issues = [_issue(7)]
    run = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", None)
    run.state = "failed"
    await dbmod.save_run(db, run)
    await it.upsert_task(db, "o/r", 7, "T", None)
    await it.set_run(db, "o/r", 7, run.id)
    await it.set_state(db, "o/r", 7, it.RUNNING)
    sched = Scheduler(db=db, settings=FakeSettings(), gh=gh, worker=FakeWorker())
    await sched.tick("o/r")
    assert (await it.get_task(db, "o/r", 7)).state == it.FAILED
    assert ["loop:failed"] in gh.labels_added


async def test_tick_needs_info_returns_on_new_comment(db):
    gh = FakeGitHub()
    gh.branch_shas["main"] = "base"
    gh.ready_issues = [_issue(7)]
    await it.upsert_task(db, "o/r", 7, "T", None)
    await it.set_state(db, "o/r", 7, it.NEEDS_INFO)
    sched = Scheduler(db=db, settings=FakeSettings(), gh=gh, worker=FakeWorker())
    await sched.tick("o/r")            # no comments yet -> still parked
    assert (await it.get_task(db, "o/r", 7)).state == it.NEEDS_INFO
    gh.issue_comments[7] = [{"user": {"login": "author"}, "body": "Postgres."}]
    await sched.tick("o/r")
    assert (await it.get_task(db, "o/r", 7)).state == it.RUNNING  # relaunched


async def test_tick_survives_github_errors(db):
    class BrokenGH(FakeGitHub):
        async def list_ready_issues(self, repo, label="loop:ready"):
            raise RuntimeError("boom")
    sched = Scheduler(db=db, settings=FakeSettings(), gh=BrokenGH(),
                      worker=FakeWorker())
    await sched.tick("o/r")  # must not raise
