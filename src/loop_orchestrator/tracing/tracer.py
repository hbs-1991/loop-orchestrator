"""What the pipeline calls.

Span ids here are **derived**, not random: `run_span_id(7)` and
`stage_span_id(7, "review")` are the same in every coroutine and after every
restart. That is what lets a stage emitted an hour after the pause still name its
parent without the Pipeline — shared by four concurrent Runs — keeping a tree in
memory and having to unwind it on every failure path.
"""
import hashlib
import logging
from datetime import datetime, timezone

from .. import db as dbmod
from ..secrets import load_repo_secrets
from .model import Span, trace_id_for_run
from .pricing import load_overrides
from .redact import Redactor
from .session_parser import parse_session

log = logging.getLogger(__name__)


def _derived_id(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def run_span_id(run_id: int) -> str:
    return _derived_id("loop-run-span", run_id)


def stage_span_id(run_id: int, stage: str) -> str:
    return _derived_id("loop-stage-span", run_id, stage)


def _parse_db_ts(value: str | None) -> int:
    """`runs.created_at` ('YYYY-MM-DD HH:MM:SS', UTC) -> ns."""
    if not value:
        return 0
    try:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1e9)
    except (ValueError, TypeError):
        return 0


def now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1e9)


class RunTracer:
    """Best-effort throughout: a trace is an observation of the work, never a
    precondition for it. Every public method swallows its own failures."""

    def __init__(self, otlp, settings, db):
        self.otlp = otlp
        self.settings = settings
        self.db = db
        self._prices = load_overrides(getattr(settings, "model_prices", ""))

    def _redactor(self, repo: str) -> Redactor:
        try:
            values = load_repo_secrets(self.settings.secrets_dir, repo).values()
        except Exception:  # noqa: BLE001 — no secrets file is normal
            values = []
        return Redactor(values)

    async def trace_agent_task(self, run, sb, stage: str, *, fresh: bool | None,
                               model: str, start_ns: int, end_ns: int,
                               status: str = "ok", error: str = "") -> None:
        """Pull the session that just ran, turn it into spans, export, roll up."""
        from .collector import fetch_session  # local: keeps the import graph flat

        trace_id = trace_id_for_run(run.id)
        stage_span = Span(name=f"stage.{stage}", trace_id=trace_id,
                          span_id=stage_span_id(run.id, stage),
                          parent_id=run_span_id(run.id),
                          start_ns=start_ns, end_ns=end_ns or now_ns())
        stage_span.set(**{
            "loop.stage": stage, "loop.run_id": run.id, "loop.repo": run.repo,
            "agent.model": model or "(agent default)",
        })
        if fresh is not None:
            stage_span.set(**{"session.fresh": fresh})
        if status != "ok":
            stage_span.fail(error or "stage failed")

        spans = [stage_span]
        trace = None
        try:
            raw = await fetch_session(sb, run.sandbox_id, stage)
            if raw:
                trace = parse_session(
                    raw, redactor=self._redactor(run.repo),
                    preview_chars=self.settings.trace_preview_chars,
                    trace_id=trace_id, parent_id=stage_span.span_id,
                    fresh=fresh, stage=stage, prices=self._prices)
        except Exception as e:  # noqa: BLE001
            log.warning("trace: could not build the session subtree for run=%s "
                        "stage=%s", run.id, stage, exc_info=True)
            await self._note(run, f"tracing: session parse failed ({e!r})")

        if trace is not None:
            spans.extend(trace.spans)
            stage_span.set(**{
                "session.api_calls": trace.api_calls,
                "session.tool_calls": trace.tool_calls,
                "cost.usd": round(trace.cost, 6),
                "tokens.input": trace.tokens.get("input", 0),
                "tokens.cache_write": trace.tokens.get("cache_write", 0),
                "tokens.cache_read": trace.tokens.get("cache_read", 0),
                "tokens.output": trace.tokens.get("output", 0),
            })
            await self._rollup(run, stage, trace, fresh, model)

        await self._export(spans)

    async def emit_run_span(self, run, outcome: str = "") -> None:
        """The root. Re-emitted on every pass through `process`, deliberately:
        a revise runs the stages again and the Run is genuinely longer than it
        was, and the derived span id means the later emission updates the same
        span rather than forking the trace."""
        trace_id = trace_id_for_run(run.id)
        span = Span(name=f"run #{run.id}", trace_id=trace_id,
                    span_id=run_span_id(run.id), parent_id=None,
                    start_ns=_parse_db_ts(getattr(run, "created_at", None)) or now_ns(),
                    end_ns=now_ns())
        span.set(**{
            "loop.run_id": run.id, "loop.repo": run.repo, "loop.kind": run.kind,
            "loop.pr_number": run.pr_number, "loop.state": run.state,
            "loop.lane": run.lane, "loop.approval_mode": run.approval_mode,
            "loop.outcome": outcome or run.state,
        })
        if run.error:
            span.fail(run.error[:500])
        try:
            roll = await dbmod.trace_rollup_for_run(self.db, run.id)
        except Exception:  # noqa: BLE001
            roll = None
        if roll:
            span.set(**{
                "cost.usd": round(roll["cost_usd"], 6),
                "session.api_calls": roll["api_calls"],
                "session.tool_calls": roll["tool_calls"],
                "tokens.cache_write": roll["tokens_cache_write"],
                "tokens.cache_read": roll["tokens_cache_read"],
                "tokens.output": roll["tokens_output"],
            })
        await self._export([span])

    async def _rollup(self, run, stage, trace, fresh, model) -> None:
        try:
            await dbmod.save_stage_cost(
                self.db, run.id, stage, model or trace.model, fresh,
                trace.api_calls, trace.tool_calls, trace.tokens, trace.cost)
            await dbmod.refresh_run_trace(self.db, run.id, trace_id_for_run(run.id))
        except Exception:  # noqa: BLE001
            log.warning("trace: rollup failed for run=%s stage=%s", run.id, stage,
                        exc_info=True)

    async def _export(self, spans) -> None:
        try:
            await self.otlp.export(spans)
        except Exception:  # noqa: BLE001 — OTLPClient already swallows, belt and braces
            log.warning("trace: export failed", exc_info=True)

    async def _note(self, run, detail: str) -> None:
        try:
            await dbmod.add_event(self.db, run.id, run.state, run.state, detail)
        except Exception:  # noqa: BLE001
            pass
