import asyncio
import logging

from . import db as dbmod
from .clients.telegram import gate_kb
from .models import (
    ACTIVE_STATES,
    AWAITING_APPROVAL,
    CONTRACTING,
    DONE,
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

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, db, settings, pipeline):
        self.db = db
        self.settings = settings
        self.pipeline = pipeline
        self.scheduler = None  # set by the app lifespan; ticked after every run
        self.actions = None    # set by the app lifespan; reads the merge gate
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

    async def repaint_merge_buttons(self, run) -> None:
        """Redraw one run's merge keyboard from the live gate.

        Painted unconditionally rather than diffed against a local memo:
        `_run_action` clears this keyboard after every press, so a memo would
        decide "unchanged" and leave the message with no buttons at all.
        Telegram answers "not modified" for a repaint that changes nothing,
        which `set_buttons` treats as success.
        """
        if (self.actions is None or run.merged_at or not run.pr_number
                or not run.tg_merge_message_id):
            return
        try:
            g = await self.actions.gate(run)
            await self.pipeline.tg.set_buttons(
                run.tg_merge_message_id,
                gate_kb(run.id, g.state, list(g.red), g.done, g.total))
        except Exception:  # noqa: BLE001 — a button is never worth a crash
            log.warning("merge button repaint failed for run=%s", run.id,
                        exc_info=True)

    async def refresh_merge_buttons_once(self) -> None:
        """Sweep every finished run's keyboard.

        The `check_run` webhook is what makes the buttons follow CI promptly;
        this sweep is the safety net behind it — a delivery GitHub dropped, or
        one that arrived while the orchestrator was restarting, would otherwise
        leave a keyboard frozen until someone pressed the indicator.
        """
        for run in await dbmod.runs_in_states(self.db, {DONE}):
            await self.repaint_merge_buttons(run)

    async def _reap_loop(self) -> None:
        while True:
            try:
                await self.reap_expired_once()
            except Exception:  # noqa: BLE001 — the reaper must survive anything
                pass
            try:
                await self.refresh_merge_buttons_once()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(60)

    async def recover(self) -> None:
        # queued: not started yet; planning/executing/reviewing/e2e_testing/
        # contracting: restartable — _planning starts a fresh planner iteration,
        # _execute re-polls its task, _review starts a fresh review iteration,
        # _e2e starts a fresh e2e iteration, _contracting re-captures the
        # contract and overwrites the row it keys.
        for run in await dbmod.runs_in_states(
                self.db, {QUEUED, PLANNING, EXECUTING, REVIEWING, E2E_TESTING,
                          CONTRACTING}):
            self.enqueue(run.id)
        # Other active steps are not idempotent — fail honestly with a hint.
        # awaiting_approval is deliberately in neither set: it is persistent.
        for run in await dbmod.runs_in_states(
                self.db, {PREPARING, STAGING, PUBLISHING, REPORTING}):
            await self.pipeline.fail(
                run, run.state,
                "the orchestrator restarted mid-step — the run was stopped; "
                "re-apply the loop:run label to retry")
