import asyncio

from . import db as dbmod
from .models import (
    ACTIVE_STATES,
    AWAITING_APPROVAL,
    E2E_TESTING,
    EXECUTING,
    PLANNING,
    PREPARING,
    PUBLISHING,
    QUEUED,
    REPORTING,
    REVIEWING,
    STAGING,
)


class Worker:
    def __init__(self, db, settings, pipeline):
        self.db = db
        self.settings = settings
        self.pipeline = pipeline
        self.scheduler = None  # set by the app lifespan; ticked after every run
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._consumers: list[asyncio.Task] = []
        self._reaper: asyncio.Task | None = None

    def enqueue(self, run_id: int) -> None:
        self._queue.put_nowait(run_id)

    async def start(self) -> None:
        for _ in range(self.settings.max_concurrent_runs):
            self._consumers.append(asyncio.create_task(self._consume()))
        self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        tasks = [*self._consumers]
        if self._reaper is not None:
            tasks.append(self._reaper)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._consumers.clear()
        self._reaper = None

    async def _consume(self) -> None:
        while True:
            run_id = await self._queue.get()
            run = None
            try:
                run = await dbmod.get_run(self.db, run_id)
                if run is not None and run.state in ACTIVE_STATES:
                    await self.pipeline.process(run)
            except Exception:  # noqa: BLE001 — pipeline reports failures itself; worker keeps living
                pass
            finally:
                self._queue.task_done()
                if self.scheduler is not None and run is not None:
                    try:
                        await self.scheduler.tick(run.repo)
                    except Exception:  # noqa: BLE001 — ticks never kill a consumer
                        pass

    async def reap_expired_once(self) -> None:
        """Tear down sandboxes of paused runs whose preview TTL has passed."""
        now = dbmod.utcnow()
        for run in await dbmod.runs_in_states(self.db, {AWAITING_APPROVAL}):
            if run.app_id and run.sandbox_expires_at and run.sandbox_expires_at <= now:
                await self.pipeline.expire_preview(run)

    async def _reap_loop(self) -> None:
        while True:
            try:
                await self.reap_expired_once()
            except Exception:  # noqa: BLE001 — the reaper must survive anything
                pass
            await asyncio.sleep(60)

    async def recover(self) -> None:
        # queued: not started yet; planning/executing/reviewing/e2e_testing:
        # restartable — _planning starts a fresh planner iteration, _execute
        # re-polls its task, _review starts a fresh review iteration,
        # _e2e starts a fresh e2e iteration.
        for run in await dbmod.runs_in_states(
                self.db, {QUEUED, PLANNING, EXECUTING, REVIEWING, E2E_TESTING}):
            self.enqueue(run.id)
        # Other active steps are not idempotent — fail honestly with a hint.
        # awaiting_approval is deliberately in neither set: it is persistent.
        for run in await dbmod.runs_in_states(
                self.db, {PREPARING, STAGING, PUBLISHING, REPORTING}):
            await self.pipeline.fail(
                run, run.state,
                "the orchestrator restarted mid-step — the run was stopped; "
                "re-apply the loop:run label to retry")
