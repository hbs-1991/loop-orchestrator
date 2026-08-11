# Agent tracing over OpenTelemetry — design

Date: 2026-08-06
Status: implemented 2026-08-06 (wiki ingest pending); not yet exercised against a live Run

## The problem

We can see that a Run cost money. We cannot see *where* the money went.

Profiling two live Runs by hand (2026-08-06) took a throwaway parser, an ssh session and
several hours, and it produced the three findings that drove that day's work: every stage
inherited the previous stage's session, an idle wait longer than the prompt-cache TTL
re-billed the whole context at write price, and a single `Read` of a large plan file added
32k tokens that were then re-sent on all 148 remaining calls of that session.

None of that is visible from the outside. `run_events` records state transitions and
`runs.summary` records the agent's closing message; between them there is nothing about
what the agent actually did. Every question worth asking — which tool burned the context,
what a fresh session starts with, whether the model switch cost what we think it costs —
currently requires the same manual archaeology, on data that is deleted when the Run ends.

## What this delivers

A trace per Run, in Jaeger, with four levels:

```
run #42                          repo, PR, lane, outcome
└── stage.execute                model, fresh-or-resumed, timeout, outcome
    └── agent.session            session id, token totals, cost, opening prompt
        └── api.call #7          context size, delta, cache read/write, cost
            └── tool.Read        args, result size, result preview
```

The questions it answers directly:

| Question | Where the answer is |
|---|---|
| Which tools did the agent use, and what came back? | `tool.*` spans — name, args, result size, preview, error flag |
| What context does a fresh session start with? | `api.call #1`'s `context.tokens`, before the agent has done anything |
| How does context grow, and what grows it? | `context.delta` on each call, attributed to the tools of the preceding call |
| Is a session fresh or inherited, and why? | `session.fresh`, set from the `continue` we sent — not guessed |
| What did it cost, and which step was expensive? | `cost.usd` on every level, from one tool call up to the whole Run |

## Where the data comes from

**The session JSONL Claude Code writes inside the sandbox.** One line per event: every API
response with its full usage breakdown (`input` / `cache_creation` / `cache_read` /
`output`), every `tool_use` with its arguments, every `tool_result` with its content, and
the opening prompt verbatim.

Two facts about that file drive the whole collection design:

- **It is outside the files API.** `internal/api/v1_files.go` roots every path at
  `<mount>/workspace/app` (`appSubdir`) and guards against symlink escapes
  (`realpathWithin`, CWE-59). The sessions live at
  `/home/sandbox/.claude/projects/<slug>/<uuid>.jsonl` — HOME, not the workspace.
  `list_files` and `read_file` cannot reach them.
- **`exec_cmd` can.** It runs a command as the image's user (`sandbox`, uid 1000), which
  owns that directory.

So: `exec_cmd` copies the file into the app directory, and the files API carries it out.
Pushing megabytes through `exec_cmd`'s stdout — a JSON string field — would work but is the
wrong endpoint for a bulk transfer, and the copy costs one `cp`.

The destination is `.loop/trace/<stage>.jsonl`. `.loop/` already holds `secrets.env` next to
a `.gitignore` containing `*`, so the copy is invisible to git and cannot reach a commit.

### Native Claude Code telemetry: considered, rejected for now

Claude Code 2.1.220 in the sandbox image supports `CLAUDE_CODE_ENABLE_TELEMETRY` with the
usual `OTEL_*` variables. It was rejected as the primary source for three reasons:

1. **The env cannot be set.** `v1TaskSubmitReq` carries only `prompt / agent / model /
   timeout_s / continue`. The runtime's `StartTaskRequest` has an `Env` map, but the v1 API
   does not expose it. The only way in is a `.claude/settings.local.json` written into the
   sandbox — which collides with target repositories that ship their own settings.
2. **It is flat.** Metrics and log events, not a structure. It reports token counts; it does
   not report that *this* tool result is what those tokens are made of.
3. **The endpoint must be reachable from inside the sandbox**, which is a sandboxd network
   question we do not need to answer to get the data we want.

Nothing here forecloses it. If a live feed is wanted later it can be added alongside and
stitched on `session_id`.

## Locked decisions

1. **Post-hoc, one stage of latency.** Spans are emitted after a stage finishes, with
   backdated timestamps, so the trace reads as if it were live. Accepted explicitly by the
   user: a live feed is not worth the settings injection above.

2. **Metadata plus truncated previews.** Every span carries sizes, counts, tokens and cost.
   Content is capped at `LOOP_TRACE_PREVIEW_CHARS` (default 500) per field. Full prompts and
   full tool results are never exported — a single Run's tool output runs to megabytes of
   repository code and command output.

3. **Secrets are redacted by value.** Every value in the repo's `secrets/<owner>__<repo>.env`
   is replaced with `***` by substring match on every preview before it leaves the process.
   The values already never enter a prompt; this covers the case of an agent echoing one
   into a command's output.

4. **A hand-rolled OTLP/HTTP emitter, not the OpenTelemetry SDK.** The spans are
   reconstructed from a file after the fact, with timestamps, trace ids and parent ids we
   compute ourselves. The SDK is built around ambient context in a live process and would be
   fought at every step, and it brings three packages into an image that currently has six
   dependencies. The OTLP/HTTP JSON encoding is a documented wire format; emitting it with
   `httpx` is about a hundred lines and is testable with `respx`, exactly like every other
   client in `clients/`.

5. **Tracing never fails a Run.** Every collection and export step is best-effort and
   swallows its exceptions, with a `run_events` note on failure. A trace is an observation of
   the work, never a precondition for it.

6. **Off by default.** No `LOOP_OTLP_ENDPOINT` means no collection, no copy, no exec calls —
   the pipeline behaves exactly as it does today. This keeps the feature out of the way of
   anyone running the orchestrator without a collector.

7. **Jaeger all-in-one** in `docker-compose.yml`, badger storage on a volume, retention
   `LOOP_TRACE_RETENTION_HOURS` (default 336, i.e. 14 days). Chosen over a Grafana or ClickHouse stack
   because the host has 2 cores and one Run already takes a whole core and 3.5 GB
   ([[ops/vps]]). Aggregate questions — "what did we spend this week" — are answered from the
   per-Run rollup written into SQLite, not from Jaeger.

8. **A per-Run rollup in SQLite.** A `run_traces` row per Run and a `run_stage_costs` row per
   stage: tokens by kind, cost, api calls, tool calls. This survives Jaeger retention, needs
   no query language, and is what the Telegram card and any future dashboard will read.

## Cost model

Prices live in one table in `tracing/pricing.py`, keyed by model id, overridable through
`LOOP_MODEL_PRICES` (JSON) so a price change does not need a code change. Cost is computed
per API call as
`input*p_in + cache_creation*p_write + cache_read*p_read + output*p_out`, all per million.
A model absent from the table yields `cost.usd = 0` and sets `cost.unpriced = true` on the
span rather than guessing.

## The trap that already bit us

**One API response is written to the JSONL as several lines sharing `message.id`, each
carrying an identical `usage` block.** Counting lines double-counts every token and every
dollar — the first hand-written profiler reported 397 API calls where there were 209.
Deduplication by `message.id` is a correctness requirement of the parser, not an
optimisation, and it gets a test of its own.

## Out of scope

- Backfilling past Runs. Checked on 2026-08-06: zero `*.jsonl` under the sandboxd data
  directory and no live sandboxes. Apps are deleted at the end of a Run and take their
  workspaces with them, so there is no history to load. Tracing starts with the next Run.
- Tracing the sub-agents a stage spawns as sidechains. The JSONL marks them
  (`isSidechain`, `parentUuid`); folding them into the tree is a follow-up.
- Sampling. At a few Runs a day, everything is traced.

## Open questions

- Does the sandbox's network reach the collector? Irrelevant today (the orchestrator exports,
  not the sandbox) but it decides whether native telemetry is ever available.
- sandboxd already computes `runtime.TokenUsage` in `TaskResult` but does not expose it in
  the v1 API. If it ever does, it becomes a cross-check against our own numbers.
