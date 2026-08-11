# 0014 — A hand-rolled OTLP/HTTP emitter, not the OpenTelemetry SDK

Date: 2026-08-06 · Status: accepted

## Context

Agent tracing ([[components/tracing]]) reconstructs spans from a session JSONL **after** the
work has finished. It needs to choose its own timestamps (backdated to when the agent
actually ran), its own trace ids (derived from the run id, so a Run recovered after a restart
or revised hours later stays in one trace) and its own parent ids (derived per stage, so
nothing has to be held in memory across concurrent Runs).

## Decision

Emit OTLP/HTTP JSON directly with `httpx` — `clients/otlp.py`, about a hundred lines — rather
than take `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http`.

## Why

- **The SDK's model is the opposite of ours.** It is built around ambient context in a live
  process: a span is started, becomes current, and ends when the work ends. Everything we do
  is explicit and after the fact. Backdating and id injection are possible there, but every
  one of them is a fight with the design.
- **Dependencies.** The project has six runtime dependencies and no Celery, no Redis
  ([[overview]]). Three more packages for a JSON POST is the wrong trade.
- **The wire format is documented and stable.** `resourceSpans` → `scopeSpans` → `spans`,
  with typed attribute values and 64-bit fields as strings. Two things are easy to get wrong
  and both have tests: a nanosecond timestamp must be a **string** (a JSON number loses it),
  and `bool` must be checked before `int` (in Python a bool *is* an int, and `True` encoded as
  `intValue` loses the distinction in the viewer).
- **It tests like the rest of `clients/`.** An injected `httpx.AsyncClient`, `respx` for the
  wire, `with_retries` for 5xx, and `export()` returning `False` instead of raising — the same
  contract as the GitHub, sandboxd and Telegram clients.

## Consequences

- No vendor-neutral instrumentation of the FastAPI app comes for free; if request-level
  tracing of the orchestrator's own HTTP surface is ever wanted, that is a separate decision.
- If the OTLP JSON encoding changes incompatibly we own the fix. It is a stable format and the
  encoder is one function with tests per field type.
- Jaeger accepts the payload as-is on `:4318`; so does any OTLP collector, so the choice of
  backend stays open.

## Links

[[components/tracing]] · [[components/clients]] · [[ops/vps]]
