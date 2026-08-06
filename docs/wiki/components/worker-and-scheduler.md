# Component: worker and scheduler — queue and backlog

- **Files:** `src/loop_orchestrator/worker.py`, `scheduler.py`, `issue_tasks.py`
- **Tests:** `tests/test_worker.py`, `test_scheduler.py`, `test_scheduler_bootstrap.py`,
  `test_issue_tasks.py`
- **Related:** [[concepts/run-lifecycle]] · [[concepts/resilience]]

## Worker

An in-process asyncio queue: `LOOP_MAX_CONCURRENT_RUNS` consumers (**2** on the VPS, see
[[concepts/resilience]]), `recover()` on startup picks up Runs left in active states, `_reap_loop`
finishes off the sandboxes of expired pauses (`expire_preview`).

No Celery, no Redis — the state lives entirely in SQLite, and a restart is survived via `recover()`.

## Scheduler — backlog mode on top of GitHub Issues

`tick(repo, seed_issues=...)` is idempotent and is called from two places: from the webhook (issue
labeled / unlabeled / closed / reopened) and from a poller every `LOOP_BACKLOG_POLL_MINUTES` for the
repositories listed in `LOOP_BACKLOG_REPOS`.

Inside a tick: `_sync` (mirrors `loop:ready` issues into the `issue_tasks` table) → `_check_answered`
(has the task come back from `needs_info`?) → `_resolve_running` → `_launch_ready`
(`pick_candidates`).

**Lanes.** The `loop:lane:<name>` label: same lane — queued, different lanes — in parallel, no lane —
an exclusive task. An accepted limitation: exclusive tasks can starve (option A from the phase-5
spec).

**Blocking** uses native GitHub dependencies: `GET /repos/{r}/issues/{n}/dependencies/blocked_by`.
They work **across repositories** (verified by the smoke test of 2026-08-04: a backend issue blocked
a frontend issue in another repo). `POST` takes `{"issue_id": <id>}` — the global id, **not** the
number; via `gh api` that is `-F issue_id=...` (a number), `-f` would send a string.

**Unblocking is caught by the poller, not by the webhook:** a `closed` event ticks only its own
repository.

## Gotchas

- **GitHub label indexing lag.** The `labeled` webhook arrives before the label shows up in the
  `?labels=loop:ready` listing — the tick saw an empty list and quietly finished, and with no row in
  `issue_tasks` the repository was not polled at all. The fix: the webhook passes the issue from the
  payload through `seed_issues`, and `_sync` merges the seed with the listing — the row appears
  within ~1 s (`9b75316`). The lag also applies to **label removal**: while cleaning up a probe a Run
  started on an already-closed issue.
- **`upsert_task` must not bump `updated_at` on every tick** — otherwise the `since` anchor drifts
  and the task never comes back from `needs_info`. It changes only when the title/lane changes (a
  phase-5a Advisor finding).
- Returning a failed task to the backlog takes two steps: remove `loop:ready` (which moves it to
  `withdrawn`) and add it again.
- The `loop:needs-info` label **does not exist**: when data is missing the planner only leaves a
  comment, "Loop planner needs more information" (`scheduler.QUESTION_MARKER`).

## Connections

`Scheduler` creates planning Runs and puts them into `Worker`; `Worker` drives `Pipeline`.
