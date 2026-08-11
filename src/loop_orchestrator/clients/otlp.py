"""OTLP/HTTP JSON exporter.

Follows the shape of every other client here: an optional injected
`httpx.AsyncClient` so tests can drive it through respx, `with_retries` for
transient failures, and no exception escaping to the caller — a trace is an
observation of the work, never a precondition for it.

Why not the OpenTelemetry SDK: these spans are rebuilt from a file after the fact,
with timestamps, trace ids and parent ids computed by us. The SDK's model is
ambient context in a live process, and it brings three packages into an image with
six dependencies. See the spec's "hand-rolled OTLP emitter" decision.
"""
import logging

import httpx

from .retry import with_retries

log = logging.getLogger(__name__)

# OTLP status codes: 0 unset, 1 ok, 2 error.
_STATUS = {"ok": 1, "error": 2}


def _attr_value(v) -> dict:
    # bool before int: bool IS an int in Python, and encoding True as intValue
    # loses the distinction in the trace viewer.
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def encode_spans(spans, service_name: str) -> dict:
    out = []
    for s in spans:
        span = {
            "traceId": s.trace_id,
            "spanId": s.span_id,
            "name": s.name,
            "kind": 1,  # SPAN_KIND_INTERNAL
            # 64-bit values must be strings in OTLP/JSON: a nanosecond timestamp
            # exceeds what JSON numbers can carry without loss.
            "startTimeUnixNano": str(s.start_ns),
            "endTimeUnixNano": str(s.end_ns or s.start_ns),
            "attributes": [{"key": k, "value": _attr_value(v)}
                           for k, v in s.attributes.items()],
            "status": {"code": _STATUS.get(s.status, 0)},
        }
        if s.parent_id:
            span["parentSpanId"] = s.parent_id
        if s.error_message:
            span["status"]["message"] = s.error_message
        out.append(span)
    return {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": service_name}},
            ]},
            "scopeSpans": [{
                "scope": {"name": "loop-orchestrator/tracing"},
                "spans": out,
            }],
        }],
    }


class OTLPClient:
    def __init__(self, endpoint: str, service_name: str = "loop-orchestrator",
                 client: httpx.AsyncClient | None = None, timeout: float = 10.0):
        self.endpoint = endpoint.rstrip("/")
        self.service_name = service_name
        self._client = client
        self._timeout = timeout

    async def _post(self, payload: dict) -> None:
        async def once():
            client = self._client or httpx.AsyncClient(timeout=self._timeout)
            try:
                r = await client.post(f"{self.endpoint}/v1/traces", json=payload,
                                      timeout=self._timeout)
                r.raise_for_status()
            finally:
                if self._client is None:
                    await client.aclose()

        await with_retries(once)

    async def export(self, spans) -> bool:
        """True when the collector accepted the batch. Never raises."""
        spans = [s for s in spans if s is not None]
        if not spans:
            return True
        try:
            await self._post(encode_spans(spans, self.service_name))
            return True
        except Exception:  # noqa: BLE001 — losing a trace must not fail a run
            log.warning("OTLP export of %d spans failed", len(spans), exc_info=True)
            return False
