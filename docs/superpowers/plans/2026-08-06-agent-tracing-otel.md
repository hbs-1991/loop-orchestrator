# Agent tracing over OpenTelemetry — implementation plan

Spec: [`../specs/2026-08-06-agent-tracing-otel.md`](../specs/2026-08-06-agent-tracing-otel.md)

TDD throughout: the test comes first and fails, then the code. Tick a box only when
`python -m pytest tests -v` is green for the whole suite, not just the new file.

The first five tasks touch disjoint files and can run in parallel. Tasks 6-9 integrate and
must follow them in order.

---

## Task 1 — the span model

- [x] `src/loop_orchestrator/tracing/__init__.py`, `src/loop_orchestrator/tracing/model.py`
- [x] `tests/test_tracing_units.py` (model, pricing and redaction share one file)

A `Span` dataclass: `name`, `span_id`, `parent_id`, `trace_id`, `start_ns`, `end_ns`,
`attributes: dict`, `status` (`ok` | `error`), `error_message`.

Ids are hex strings — 32 chars for a trace, 16 for a span — generated from `secrets.token_hex`.
A `trace_id_for_run(run_id)` helper derives a **stable** trace id from the run id
(`sha256(f"loop-run-{run_id}")[:32]`) so a Run resumed after an orchestrator restart, or
revised hours later, lands in the same trace instead of starting a second one.

Tests: ids have the right width and are hex; the same run id yields the same trace id twice;
two different run ids do not collide; `duration_ms` is derived from the timestamps.

## Task 2 — the price table

- [x] `src/loop_orchestrator/tracing/pricing.py`
- [x] covered by `tests/test_tracing_units.py`

`PRICES: dict[str, Price]` per million tokens — input, cache write (5m), cache read, output.
Seeded with `claude-opus-5` (5 / 6.25 / 0.5 / 25), `claude-fable-5` (10 / 12.5 / 1 / 50),
`claude-sonnet-5` (3 / 3.75 / 0.3 / 15), `claude-haiku-4-5`.
`cost_usd(model, usage) -> tuple[float, bool]` returns the cost and whether the model was
priced. `load_overrides(json_str)` merges `LOOP_MODEL_PRICES`.

Tests: a known model prices each token kind at its own rate; an unknown model returns
`(0.0, False)`; an override replaces one model and leaves the rest; malformed override JSON is
ignored rather than raised.

## Task 3 — redaction and previews

- [x] `src/loop_orchestrator/tracing/redact.py`
- [x] covered by `tests/test_tracing_units.py`

`Redactor(secret_values: Iterable[str])` with `preview(text, limit)`:
collapses whitespace, replaces every secret value with `***`, truncates to `limit` and appends
an ellipsis. Values shorter than 4 characters are ignored — redacting `"1"` would blot out
every digit in every preview.

Tests: a secret in the middle of a command is replaced; a secret spanning the truncation
boundary is replaced **before** truncation, never after; the empty secret set is a no-op;
short values are skipped; the result never exceeds the limit.

## Task 4 — the session parser

- [x] `src/loop_orchestrator/tracing/session_parser.py`
- [x] `tests/test_tracing_session_parser.py` — the fixture is built inline, not checked in:
      every quirk it exercises needed a sentence saying why

`parse_session(raw: bytes, *, redactor, preview_chars, cache_ttl_s=300) -> SessionTrace`
holding the session span, its `api.call` children and their `tool.*` grandchildren.

Rules, each with a test:

- **Deduplicate by `message.id`.** Several assistant lines share one id and repeat one
  `usage`; they are one API call. This is the bug that made the first hand-written profiler
  report 397 calls for 209.
- `context.tokens = input + cache_creation + cache_read`; `context.delta` against the
  previous call, and equal to `context.tokens` when the previous is larger (a reset).
- `cache.miss = true` when `cache_read == 0` on any call after the first.
- `idle_before_s` from the gap to the previous call's end; `cache.expired_while_idle = true`
  when it exceeds `cache_ttl_s`.
- Tool results are matched to their `tool_use` by `tool_use_id`, carrying `result.chars`,
  `result.preview` and `tool.error`.
- A truncated or malformed line is skipped, not raised: the file is read from a sandbox that
  may have been killed mid-write.

The fixture is hand-written, small, and contains: a split-across-lines assistant message, a
tool call with a matching result, a tool error, a cache-miss call, and one malformed line.

## Task 5 — the OTLP/HTTP client

- [x] `src/loop_orchestrator/clients/otlp.py`
- [x] `tests/test_otlp_client.py`

`OTLPClient(endpoint, service_name, client=None).export(spans) -> bool`, posting OTLP JSON to
`{endpoint}/v1/traces`. Follows the conventions of `clients/`: an optional injected
`httpx.AsyncClient`, `with_retries` for transient failures, and no exception reaching the
caller — `export` returns `False` instead.

Timestamps go out as nanosecond strings (the JSON encoding requires a string for a 64-bit
integer). Attribute values are typed into `stringValue` / `intValue` / `doubleValue` /
`boolValue`.

Tests, all through `respx`: the payload shape (`resourceSpans` -> `scopeSpans` -> `spans`);
parent ids are present on children and absent on the root; each attribute type is encoded to
the right variant; a 500 is retried; an unreachable endpoint returns `False` and raises
nothing.

## Task 6 — pulling the session out of the sandbox

- [x] `src/loop_orchestrator/tracing/collector.py`
- [x] `tests/test_tracing_collector.py`

`fetch_session(sb, sandbox_id, stage) -> bytes | None`:

1. `exec_cmd` runs a one-liner that finds the newest `*.jsonl` under
   `$HOME/.claude/projects/`, and copies it to `.loop/trace/<stage>.jsonl` in the app
   directory. Remember `exec_cmd` passes neither `-u` nor `-w`: the command must `cd` into
   the app directory itself.
2. `read_file` pulls that path through the files API.

Why the copy: the files API is rooted at `<mount>/workspace/app` with symlink-escape guards,
so `$HOME/.claude` is unreachable through it, while `exec_cmd` runs as the user that owns it.

Tests with `FakeSandboxd`: the happy path returns the bytes; a non-zero exit code from the
copy returns `None`; a missing file returns `None`; nothing raises when the sandbox is gone.

## Task 7 — the SQLite rollup

- [x] `src/loop_orchestrator/db.py` — `run_traces`, `run_stage_costs`, `save_trace_rollup`,
      `trace_rollup_for_run`
- [x] `tests/test_db_tracing.py`

Two tables, created in the same idempotent `CREATE TABLE IF NOT EXISTS` block as the rest of
the schema. `run_traces`: run id, trace id, totals, cost, api calls, tool calls, updated_at.
`run_stage_costs`: run id, stage, model, fresh, the same totals per stage.

Tests: a rollup round-trips; a second write for the same Run replaces rather than duplicates
(a revise re-runs every stage); a Run with no trace returns `None`.

## Task 8 — wiring the pipeline

- [x] `src/loop_orchestrator/config.py` — `otlp_endpoint`, `trace_preview_chars` (500),
      `trace_service_name`, `model_prices`. Retention turned out to belong to Jaeger
      alone (`LOOP_TRACE_RETENTION_HOURS` in compose), so it is not a Settings field
- [x] `src/loop_orchestrator/pipeline.py`
- [x] `tests/test_pipeline_tracing.py`

`Pipeline` gains a `_tracer` that is `None` when `otlp_endpoint` is unset — and when it is
`None`, not one exec call, copy or export happens.

Emission points:

- `process` opens the `run` span and closes it in every exit path, `fail` included.
- Each stage wraps its work in a `stage.<name>` span carrying `model` and the
  `continue_session` value actually sent, so `session.fresh` is recorded rather than inferred.
- After each agent task, `fetch_session` + `parse_session` produce the subtree and the whole
  batch is exported once.

Tests: with no endpoint the pipeline makes no tracing calls at all; with one, a Run produces a
`run` span whose children are the stages; an export failure leaves the Run `done`; a parse
failure leaves the Run `done` and writes a `run_events` note.

## Task 9 — Jaeger, configuration, docs

- [x] `docker-compose.yml` — `jaeger` service, badger volume, `COLLECTOR_OTLP_ENABLED`,
      retention from `LOOP_TRACE_RETENTION_HOURS`; UI bound to 127.0.0.1 only
- [x] `.env.example` — the new variables with the format only, no host values
- [ ] `docs/wiki/` — ingest (waiting: a concurrent session is writing log.md/overview.md): `components/tracing.md`, a `decisions/` entry for the
      hand-rolled OTLP emitter, `overview.md`, `index.md`, `log.md`

The Jaeger UI must not be published to the internet: bind `127.0.0.1:16686` and reach it over
an ssh tunnel. It has no authentication of its own, and the spans carry prompt previews.

---

## Definition of done

- [x] `python -m pytest tests -v` green.
- [x] With `LOOP_OTLP_ENDPOINT` unset, the diff changes no observable behaviour.
- [ ] One live Run produces a trace in Jaeger with all four levels present, and a
      `run_traces` row whose cost is within rounding of the sum of its stages.
