import asyncio

from loop_orchestrator import db as dbmod
from loop_orchestrator.models import (
    AWAITING_APPROVAL,
    E2E_TESTING,
    EXECUTING,
    FAILED,
    PLANNING,
    PREPARING,
    QUEUED,
    REVIEWING,
    STAGING,
)
from loop_orchestrator.worker import Worker

from tests.conftest import FakeSettings


class RecordingPipeline:
    def __init__(self):
        self.processed: list[int] = []
        self.failed: list[tuple[int, str]] = []

    async def process(self, run):
        self.processed.append(run.id)
        run.state = "done"

    async def fail(self, run, stage, message):
        self.failed.append((run.id, stage))


async def test_worker_processes_enqueued(db):
    pipe = RecordingPipeline()
    w = Worker(db=db, settings=FakeSettings(), pipeline=pipe)
    run = await dbmod.create_run(db, "o/r", 1, "b")
    await w.start()
    w.enqueue(run.id)
    await asyncio.wait_for(w._queue.join(), timeout=2)
    await w.stop()
    assert pipe.processed == [run.id]


async def test_worker_skips_missing_and_inactive(db):
    pipe = RecordingPipeline()
    w = Worker(db=db, settings=FakeSettings(), pipeline=pipe)
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.state = "done"
    await dbmod.save_run(db, run)
    await w.start()
    w.enqueue(999)      # does not exist
    w.enqueue(run.id)   # not active
    await asyncio.wait_for(w._queue.join(), timeout=2)
    await w.stop()
    assert pipe.processed == []


async def test_recover(db):
    pipe = RecordingPipeline()
    w = Worker(db=db, settings=FakeSettings(), pipeline=pipe)
    q = await dbmod.create_run(db, "o/r", 1, "b")          # queued — re-enqueue
    e = await dbmod.create_run(db, "o/r", 2, "b")
    e.state = EXECUTING
    await dbmod.save_run(db, e)                             # executing — re-enqueue (resume)
    r = await dbmod.create_run(db, "o/r", 3, "b")
    r.state = REVIEWING
    await dbmod.save_run(db, r)                             # reviewing — re-enqueue (restart review)
    p = await dbmod.create_run(db, "o/r", 4, "b")
    p.state = PREPARING
    await dbmod.save_run(db, p)                             # preparing — orphaned, fail
    await w.recover()
    assert sorted(w._queue._queue) == [q.id, e.id, r.id]  # type: ignore[attr-defined]
    assert pipe.failed == [(p.id, PREPARING)]


async def test_recover_requeues_e2e_testing(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.state = E2E_TESTING
    await dbmod.save_run(db, run)
    # recover() only needs the pipeline for non-restartable states;
    # a lone e2e_testing run never touches it.
    w = Worker(db, FakeSettings(), pipeline=None)
    await w.recover()
    assert w._queue.get_nowait() == run.id


class FakePipelineReap:
    def __init__(self):
        self.expired: list[int] = []

    async def expire_preview(self, run):
        self.expired.append(run.id)


async def test_reap_expired_previews(db):
    pipeline = FakePipelineReap()
    worker = Worker(db=db, settings=FakeSettings(), pipeline=pipeline)
    expired = await dbmod.create_run(db, "o/r", 1, "b")
    expired.state = AWAITING_APPROVAL
    expired.app_id = "app-1"
    expired.sandbox_expires_at = "2000-01-01 00:00:00"
    await dbmod.save_run(db, expired)
    alive = await dbmod.create_run(db, "o/r", 2, "b")
    alive.state = AWAITING_APPROVAL
    alive.app_id = "app-2"
    alive.sandbox_expires_at = "2999-01-01 00:00:00"
    await dbmod.save_run(db, alive)
    torn_down = await dbmod.create_run(db, "o/r", 3, "b")
    torn_down.state = AWAITING_APPROVAL  # already reaped: no app, no deadline
    await dbmod.save_run(db, torn_down)
    await worker.reap_expired_once()
    assert pipeline.expired == [expired.id]


async def test_recover_requeues_planning_runs(db):
    run = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", None)
    run.state = PLANNING
    await dbmod.save_run(db, run)
    worker = Worker(db=db, settings=FakeSettings(), pipeline=None)
    enqueued: list[int] = []
    worker.enqueue = enqueued.append
    await worker.recover()
    assert run.id in enqueued


async def test_consumer_ticks_scheduler_after_run(db):
    run = await dbmod.create_run(db, "o/r", 5, "b")

    class NoopPipeline:
        async def process(self, run):
            pass

    ticks: list[str] = []

    class FakeScheduler:
        async def tick(self, repo):
            ticks.append(repo)

    worker = Worker(db=db, settings=FakeSettings(), pipeline=NoopPipeline())
    worker.scheduler = FakeScheduler()
    await worker.start()
    worker.enqueue(run.id)
    await asyncio.wait_for(worker._queue.join(), timeout=2)
    await worker.stop()
    assert ticks == ["o/r"]


async def test_recover_leaves_awaiting_approval_alone(db):
    class FailRecorder:
        def __init__(self):
            self.failed: list[int] = []

        async def fail(self, run, stage, message):
            self.failed.append(run.id)

    pipeline = FailRecorder()
    worker = Worker(db=db, settings=FakeSettings(), pipeline=pipeline)
    paused = await dbmod.create_run(db, "o/r", 1, "b")
    paused.state = AWAITING_APPROVAL
    await dbmod.save_run(db, paused)
    staging = await dbmod.create_run(db, "o/r", 2, "b")
    staging.state = STAGING
    await dbmod.save_run(db, staging)
    await worker.recover()
    assert pipeline.failed == [staging.id]      # staging fails honestly
    assert worker._queue.qsize() == 0 or paused.id not in list(worker._queue._queue)
