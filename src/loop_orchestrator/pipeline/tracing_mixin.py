"""Turning finished agent sessions into spans, without ever failing a run.

Named `tracing_mixin` rather than `tracing` so it cannot be mistaken for the
`loop_orchestrator.tracing` package it delegates to. Every method here is a
no-op when `self.tracer is None`: an orchestrator without a collector must
behave exactly as it did before this feature existed.
"""

import logging

from ..models import Run

log = logging.getLogger(__name__)


class TracingMixin:
    async def _trace_task(self, run: Run, stage: str, *, fresh: bool | None,
                          model: str, start_ns: int, status: str = "ok",
                          error: str = "") -> None:
        """Turn the agent session that just finished into spans.

        Called after the task ends, not before: the session file is only
        complete once the agent has stopped writing to it.
        """
        if self.tracer is None:
            return
        from ..tracing.tracer import now_ns
        try:
            await self.tracer.trace_agent_task(
                run, self.sb, stage, fresh=fresh, model=model,
                start_ns=start_ns, end_ns=now_ns(), status=status, error=error)
        except Exception:  # noqa: BLE001 — tracing never fails a run
            log.warning("tracing failed for run=%s stage=%s", run.id, stage,
                        exc_info=True)

    def _trace_start(self) -> int:
        if self.tracer is None:
            return 0
        from ..tracing.tracer import now_ns
        return now_ns()

    async def _emit_run_span(self, run: Run) -> None:
        if self.tracer is None:
            return
        try:
            await self.tracer.emit_run_span(run)
        except Exception:  # noqa: BLE001 — tracing never fails a run
            log.warning("tracing: run span failed for run=%s", run.id, exc_info=True)
