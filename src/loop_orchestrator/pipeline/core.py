"""The Pipeline class: the clients it holds, and the PR Run's state machine.

`process` is deliberately the only long method left — it is the one place the
order of the stages is written down, and reading it top to bottom should be
the fastest way to learn what a Run does. Every stage it calls lives in its own
module; this class only composes them.
"""

import logging
from datetime import datetime, timedelta, timezone

from .. import db as dbmod
from ..models import (
    AWAITING_APPROVAL,
    CONTRACTING,
    DONE,
    E2E_TESTING,
    EXECUTING,
    PREPARING,
    PUBLISHING,
    QUEUED,
    REPORTING,
    REVIEWING,
    STAGING,
    Run,
)
from ..state_machine import transition
from .contract_stage import ContractMixin
from .e2e_stage import E2EMixin
from .errors import ExecutionTimeout, RunFailure
from .execute import ExecuteMixin
from .gitsync import SyncMixin
from .planning_stage import PlanningMixin
from .prepare import PrepareMixin
from .preview import PreviewMixin
from .publish import PublishMixin
from .reporting import ReportingMixin
from .review_stage import ReviewMixin
from .sandbox_tasks import SandboxTasksMixin
from .tracing_mixin import TracingMixin

log = logging.getLogger(__name__)


class Pipeline(
    PrepareMixin,
    ExecuteMixin,
    ReviewMixin,
    E2EMixin,
    ContractMixin,
    PublishMixin,
    PreviewMixin,
    PlanningMixin,
    SyncMixin,
    ReportingMixin,
    SandboxTasksMixin,
    TracingMixin,
):
    def __init__(self, db, settings, gh, sb, tg, tracer=None):
        self.db = db
        self.settings = settings
        self.gh = gh
        self.sb = sb
        self.tg = tg
        # None = tracing off, and then not one exec call, copy or export happens.
        # An orchestrator without a collector must behave exactly as it did
        # before this feature existed.
        self.tracer = tracer
        if tracer is None and getattr(settings, "otlp_endpoint", ""):
            from ..clients.otlp import OTLPClient
            from ..tracing.tracer import RunTracer
            self.tracer = RunTracer(
                OTLPClient(settings.otlp_endpoint,
                           service_name=settings.trace_service_name),
                settings, db)

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
                exec_start_ns = self._trace_start()
                # A task_id already set means we did not submit this one:
                # recovery after a restart, or a revise that submitted from
                # actions.py. We do not know how that session was opened, and
                # "unknown" is the honest answer rather than a guess.
                exec_fresh = True if run.task_id is None else None
                try:
                    await self._execute(run)
                except ExecutionTimeout:
                    await self._trace_task(run, EXECUTING, fresh=exec_fresh, model="",
                                           start_ns=exec_start_ns, status="error",
                                           error="execution timed out")
                    await self.sb.cancel_task(run.sandbox_id, run.task_id)
                    await self._publish_partial(run)
                    raise RunFailure(
                        EXECUTING,
                        f"timed out after {run.timeout_minutes} minutes of agent work",
                    ) from None
                except RunFailure as f:
                    await self._trace_task(run, EXECUTING, fresh=exec_fresh, model="",
                                           start_ns=exec_start_ns, status="error",
                                           error=str(f))
                    await self._publish_partial(run)
                    raise
                await self._trace_task(run, EXECUTING, fresh=exec_fresh, model="",
                                       start_ns=exec_start_ns)
                await transition(
                    self.db, run,
                    REVIEWING if run.review_enabled
                    else E2E_TESTING if run.e2e_enabled
                    else CONTRACTING if run.contract_enabled else STAGING)
                await self._refresh_card(run)
            if run.state == REVIEWING:
                await self._review(run)
                await transition(self.db, run,
                                 E2E_TESTING if run.e2e_enabled
                                 else CONTRACTING if run.contract_enabled
                                 else STAGING)
                await self._refresh_card(run)
            if run.state == E2E_TESTING:
                await self._e2e(run)
                await transition(self.db, run,
                                 CONTRACTING if run.contract_enabled else STAGING)
                await self._refresh_card(run)
            if run.state == CONTRACTING:
                await self._contracting(run)
                await transition(self.db, run, STAGING)
                await self._refresh_card(run)
            if run.state == STAGING:
                staged = await self._stage(run)
                if staged and run.approval_mode == "always":
                    sleepable = await self._start_preview(run)
                    if not sleepable:
                        # Nothing polls during the pause, so the idle reaper
                        # would stop the sandbox — and kill a preview it cannot
                        # bring back — long before our own TTL expires. Hold it
                        # for the whole window; the worker's reaper is what
                        # actually ends the pause.
                        await self.sb.keepalive(run.sandbox_id,
                                                self.settings.preview_ttl_minutes)
                    run.sandbox_expires_at = (
                        datetime.now(timezone.utc)
                        + timedelta(minutes=self.settings.preview_ttl_minutes)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    await transition(self.db, run, AWAITING_APPROVAL)
                    await self._refresh_card(run)
                    await self._notify_awaiting(run)
                    if sleepable:
                        # Last, after the videos have been read out of the
                        # workspace: a paused Run needs no sandbox until someone
                        # opens the preview or asks for a revise, and holding
                        # ~3.5 GB for two hours outside the concurrency cap is
                        # what made the cap a fiction.
                        await self._sleep_pause(run)
                    return  # release the worker slot; approve/revise/discard re-enqueue
                await transition(self.db, run, PUBLISHING)
                await self._refresh_card(run)
            if run.state == PUBLISHING:
                await self._publish_ff(run)
                await self._publish_contract_comment(run)
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
        finally:
            # Every exit closes the root: done, failed, and the awaiting_approval
            # pause that returns early. The span id is derived from the run id, so
            # the pass after a revise updates this span instead of forking the
            # trace into a second root.
            await self._emit_run_span(run)
