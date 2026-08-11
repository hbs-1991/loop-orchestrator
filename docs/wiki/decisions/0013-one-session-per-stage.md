# 0013 — One Claude session per stage, and the `continue` tri-state

Date: 2026-08-06 · Status: accepted, deployed

## Context

Profiling two live Runs (387 API calls, ≈$120 at list prices) put **61% of the spend on cache
writes and 34% on cache reads**. Only 5% was output — the thing the agent actually produced.

The cause was one line. `SandboxdClient.submit_task` never sent the `continue` field, and its
`bool = False` default emitted nothing, so sandboxd's own default applied — and that default
is **"continue whenever the sandbox already has a session"** (`cmd/runtimed/task.go`,
`hasPriorTask`). Every stage after the first therefore inherited the previous stage's whole
session: the reviewer opened each of its calls on ~230k tokens of the executor's context, and
because the reviewer also switches model (Opus → Fable) the prompt cache missed outright, so
that entire inherited context was re-written at 1.25× price.

Two behavioural bugs were hiding in the same default, both contradicting comments in our own
code that claimed otherwise:

- the **advisor** was reviewing plans inside the **planner's** session — it was never the
  independent second opinion it was written to be;
- the preview step could only start the app because it had inherited an earlier stage's
  knowledge of the environment and secrets.

## Decision

`submit_task` takes a tri-state `continue_session: bool | None`, mirroring sandboxd's
`Continue *bool`, and **every caller states it**:

| Fresh (`False`) | Continue (`True`) |
|---|---|
| execute (first task), review, review-fix, e2e, e2e-fix, advisor, **every planner round**, git-sync resolver | rate-limit resume, transient-failure resume, `CONTINUE_PROMPT` |

`None` is left only where the platform genuinely should decide, and it is now the exception
rather than the silent default.

A stage that no longer inherits a session must be **told** what that session used to carry, so
the review, fix and e2e prompts now name the diff range, the spec and plan paths, the test
command and the secret key names. And a `WORKING_EFFICIENTLY` block — one definition in
`review.py`, imported by `pipeline.py` and `e2e.py` — states the cost rule to every agent:
do not park in a single wait past the five-minute cache TTL, read line ranges rather than
whole files, never re-read a file already read.

## Consequences

- The reviewer starts on tens of thousands of tokens instead of ~230k, and the model switch
  no longer invalidates an inherited context.
- **`revise` lost its free ride.** It resumed "the most recent session", which used to be the
  executor's because everything shared one. Now the most recent is the reviewer's or the e2e
  agent's. sandboxd can resume only the most recent session — `Continue *bool` becomes
  `claude --continue`, and there is no `--resume <id>` in the v1 API — so feedback lands in
  the executor's session only when neither review nor e2e ran, and otherwise opens a fresh one
  whose prompt restates the branch, the documents and the test command.
- **The planner's own `revise` fell to the same rule on 2026-08-08.** It was left on `continue` in the
  belief that it resumed the session that had written the documents — but the advisor runs between the
  rounds, so "the most recent session" is the advisor's, and the planner was being handed the
  reviewer's context to edit its own work from. Restoring the *planner's* session is not worth
  chasing either: an advisor round outlives the five-minute prompt cache, so an inherited context
  comes back at write price, while re-reading `.loop/task.md`, the spec and the plan is three file
  reads. `build_planner_revise_prompt` now carries what the session used to.
- Whether this actually worked is now measurable rather than argued: [[components/tracing]]
  records `session.fresh` and `session.opening_context_tokens` per stage.

## Links

[[components/pipeline]] · [[concepts/agent-steering]] · [[components/tracing]] ·
[[decisions/0005-transient-resume-budget]]
