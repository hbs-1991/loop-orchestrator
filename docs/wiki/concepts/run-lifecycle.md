# Concept: the Run lifecycle

A Run is the orchestrator's unit of work. There are **two kinds** (`Run.kind`), and they walk the same
state table along different routes.

## The two kinds of Run

| Kind | Trigger | What it does | Result |
|---|---|---|---|
| `execution` (default) | the `loop:run` label on a PR | executes the plan from the PR: execute → review → e2e → staging → pause → publish | code in the PR branch |
| `planning` | an issue labelled `loop:ready` (via the `scheduler`) | the planner ⇄ Implementor Advisor write the spec and the plan | a PR "Closes #N." labelled `loop:run` |

A planning Run has `pr_number = 0` — a sentinel, not a number. The branch is `loop/issue-N`, and the
task is written to `.loop/task.md`. This is the case where `save_run` must include `pr_number` in the
SET list: without it, a restart between `publishing` and `reporting` sent a planning Run off into the
"questions" branch (`008db01`).

## States

`queued → preparing → executing → reviewing → e2e_testing → contracting → staging →
awaiting_approval → publishing → reporting → done|failed|cancelled`, plus `planning` instead of
`executing…staging` for a planning Run. Transitions are validated by `state_machine.TRANSITIONS` and
written to `run_events` — which is also what the progress card is drawn from.

Config-driven skips: `reviewing` and `e2e_testing` per `.loop.yml`; `awaiting_approval` when
`approval: never`. `contracting` is skipped when the Run has no issue or its issue blocks nobody —
the decision is made inside the stage, so `staging` stays a legal target of all three verification
states ([[concepts/contract-handoff]]). Going back `awaiting_approval → executing` is the revise loop
(a fix requested by replying in Telegram) — and it replays `contracting`, which is exactly why that
stage stands before the pause rather than after publication.

## Invariants

- **One active Run per PR** (and per issue). Deduplication happens in the webhook under an
  `asyncio.Lock` (`app.state.dedup_lock`) — otherwise check-and-insert races with itself.
- **Every outcome ends with a Telegram message.** A failure is an outcome too.
- **An agent crash on execute is not lost work:** `_publish_partial` best-effort publishes the commits
  already made.
- **A sandbox lives exactly as long as it is needed.** It is deleted together with the app at the end of
  the Run; during the `awaiting_approval` pause it is **stopped**, not held awake, and the app is
  finished off after `LOOP_PREVIEW_TTL_MINUTES` by the reaper in `worker`. Opening the preview link
  starts the container again (traefik's wake catch-all), `revise` wakes it explicitly, and its death
  does **not** block approve/merge — the code is already in the temporary branch
  ([[concepts/publication]], [[decisions/0015-sleep-the-paused-sandbox]]).
- **The stage deadline is the only boundary.** It is monotonic: rate-limit pauses do not eat into it
  (`ExecutionTimeout`), and transient polling errors simply wait for the next tick
  ([[concepts/resilience]]).

## Recovery after a restart

`Worker.recover()` brings back Runs from active states. The delicate part is a stage that already has a
task running in the sandbox: resubmitting into a busy sandbox gives a 409 and used to kill the Run
(that is how Run#15 died). Now `_submit_resumable` first waits the stuck task out via `list_tasks`
(`_drain_stale_task`), and only then submits (`c5a3a2f`).

The `awaiting_approval` pause survives a restart by construction: the state is in SQLite, the buttons
arrive over the Telegram webhook, and the actions work even after the sandbox has died.

## Control from outside

From Telegram: approve · discard · cancel · restart · merge · merge & deploy · revise reply
([[components/ingress-and-control]]). You can cancel a Run without a Telegram client by POSTing to
`/webhooks/telegram` with the callback `cn:<run_id>`, the `X-Telegram-Bot-Api-Secret-Token` header and
an admin `from.id` — a handy debugging technique.

Returning a failed issue task to the queue takes **two steps**: remove `loop:ready` (the next tick moves
it to `withdrawn`) and put it back. There is no direct path from `failed` into the backlog.

## Links

- [[components/pipeline]] — the stage code · [[components/worker-and-scheduler]] — queue and recovery
- [[concepts/publication]] · [[concepts/resilience]] · [[components/storage-and-config]] ·
  [[concepts/contract-handoff]]
- MVP spec (states are a Locked Decision):
  [`docs/superpowers/specs/2026-07-31-loop-engineering-mvp-design.md`](../../superpowers/specs/2026-07-31-loop-engineering-mvp-design.md)
