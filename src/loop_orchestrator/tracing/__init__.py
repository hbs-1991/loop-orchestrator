"""Agent tracing: session JSONL -> OpenTelemetry spans.

Design: `docs/superpowers/specs/2026-08-06-agent-tracing-otel.md`.
"""
from .model import Span, new_span_id, trace_id_for_run
from .pricing import cost_usd
from .redact import Redactor
from .session_parser import SessionTrace, parse_session

__all__ = ["Span", "new_span_id", "trace_id_for_run", "cost_usd", "Redactor",
           "SessionTrace", "parse_session"]
