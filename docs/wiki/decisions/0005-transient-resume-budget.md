# 0005 — A transient API failure resumes instead of killing the Run

- **Status:** accepted
- **Date:** 2026-08-05
- **Related:** [[concepts/resilience]] · commits `76fb568`, `ab4e431`, `806e32e`

## Context

A broken agent stream ("API Error: Response stalled mid-stream") finished the task with an error, and
the Run failed, throwing away hours of work already done. During the Anthropic incident of 2026-08-05
(Fable/Opus/Mythos degradation) Run#32/#34/#35 died exactly that way.

The key observation: **the agent session survives the break**, so a resubmit with
`continue_session=true` continues from where it stopped instead of starting over.

## Decision

Both polling loops (`_execute`, `_run_sandbox_task`) recognise transient markers
(`TRANSIENT_AGENT_MARKERS`: api error, connection error, econnreset, socket hang up, fetch failed) and
resubmit the task continuing the session. The budget is `LOOP_AGENT_RETRY_ATTEMPTS` (**10**) per stage,
with a `LOOP_AGENT_RETRY_BACKOFF_SECONDS` (**120 s**) pause before each resume. The rate-limit branch
is checked first.

## Alternatives

- *Immediate resume with no pause* — rejected: breaks cluster (~one every 5 min), the resume jumps
  straight back into the same hole and burns the budget in a minute.
- *A budget of 2, then 4* — both turned out too small: attempts ran out faster than the provider
  recovered, and every failure threw away real progress. 10 matches Claude Code's own retry default.
- *Restart the stage from scratch* — rejected: it loses work the session already remembers.

## Consequences

- There is still exactly one boundary — the **stage deadline**; the resume budget does not widen it.
- A genuine task error (not transient) still fails the Run immediately.
- During a drawn-out provider incident the right move is not to spin retries but to restart the Run
  after the incident closes.
