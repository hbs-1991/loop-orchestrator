# Component: tracing — where a Run's money and context went

Files: `tracing/{model,pricing,redact,session_parser,collector,tracer}.py`,
`clients/otlp.py`, the `run_traces` / `run_stage_costs` tables in `db.py`.
Design and locked decisions — [the spec](../../superpowers/specs/2026-08-06-agent-tracing-otel.md).
Emitter rationale — [[decisions/0014-hand-rolled-otlp-emitter]].

## Why it exists

Before this, `run_events` recorded state transitions and `runs.summary` the agent's closing
message. Between them there was nothing about what the agent actually **did**. The
2026-08-06 cost analysis had to be done by hand — a throwaway parser over session files
pulled off the VPS by ssh — and the data it read is deleted when a Run ends.

## The shape

```
run #42                     repo, PR, lane, state, outcome, whole-Run cost
└── stage.review            model, session.fresh, timing, per-stage cost
    └── agent.session       session id, token totals, opening prompt preview
        └── api.call #7     context.tokens, context.delta, cache read/write, cost
            └── tool.Read   args, result.chars, result.preview, tool.error
```

Four attributes carry most of the value:

- **`session.opening_context_tokens`** — what a fresh session costs before the agent has done
  anything: system prompt + tool definitions + our stage prompt.
- **`context.delta`** — how much each call added. A `Read` of a 56 KB plan adds ~32k tokens
  that are then re-sent on every later call of that session; that is where the money goes.
- **`cache.miss`** and **`cache.expired_while_idle`** — the two failures
  [[decisions/0009-concurrency-cap-and-poll-resilience]]'s sibling work went after: a model
  switch invalidating the cache, and an idle wait past the five-minute TTL.
- **`session.fresh`** — taken from the `continue` value actually sent to sandboxd, never
  inferred from the file.

## How the data is collected

**The session JSONL cannot be read through the files API.** `internal/api/v1_files.go` roots
every path at `<mount>/workspace/app` and refuses symlink escapes (`realpathWithin`, CWE-59),
while Claude Code writes its sessions to `/home/sandbox/.claude/projects/<slug>/<uuid>.jsonl`
— HOME, not the workspace. `exec_cmd` runs as `sandbox` (uid 1000), which owns that
directory, so it copies the newest file into `.loop/trace/<stage>.jsonl` and `read_file`
carries it out. `.loop/` already has a `.gitignore` containing `*`
([[decisions/0002-secrets-as-file]]), so the copy cannot ride a commit.

Collection happens **after** a stage, never during: the file is only complete once the agent
has stopped writing.

## Two traps worth remembering

**One API response is several JSONL lines.** They share `message.id` and each repeats the
same `usage` block. Counting lines double-counts every token and every dollar — the first
hand-written profiler reported 397 calls where there were 209. `session_parser` deduplicates
by `message.id`, and there is a test whose only job is to keep that true.

**Fable 5 is twice Opus 5 per token** ($10/$50 against $5/$25). The reviewer runs on Fable,
so it is the expensive stage, not the cheap one — assuming otherwise inverted the conclusion
of the first analysis. `pricing.py` spells the table out rather than leaving it to intuition.

## Safety properties

- **Off unless `LOOP_OTLP_ENDPOINT` is set.** No endpoint means no exec call, no copy, no
  export — the pipeline behaves exactly as it did before the feature existed.
- **Never fails a Run.** Every step swallows its exceptions; a parse failure leaves a note in
  `run_events`. Covered by tests for a failing collector, a corrupt JSONL, a dead sandbox and
  a tracer that raises from every method.
- **Content is capped and scrubbed.** `LOOP_TRACE_PREVIEW_CHARS` (500) per field, and every
  value from `secrets/<owner>__<repo>.env` is replaced with `***` **before** truncation —
  truncating first can cut a credential in half and leave the first half in the span.
- **The Jaeger UI is bound to 127.0.0.1.** It has no authentication and the spans carry
  prompt previews; reach it over an ssh tunnel ([[ops/vps]]).

## Span ids are derived, not random

`run_span_id(7)` and `stage_span_id(7, "review")` are the same in every coroutine and after
every restart. That is what lets a stage emitted an hour after the `awaiting_approval` pause
name its parent without `Pipeline` — shared by concurrent Runs — holding a tree in memory and
unwinding it on every failure path. A revise re-emits the same root span id, so the second
pass updates the trace instead of forking it.

## What survives Jaeger

`run_traces` (one row per Run) and `run_stage_costs` (one per stage) hold tokens by kind,
cost, api calls and tool calls. Jaeger keeps the detail for `LOOP_TRACE_RETENTION_HOURS`
(336); the rollup outlives it and answers "what did we spend" with plain SQL.

## Links

[[components/pipeline]] · [[components/clients]] · [[components/storage-and-config]] ·
[[concepts/agent-steering]] · [[concepts/sandboxd-platform]]
