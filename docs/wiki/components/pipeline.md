# Component: pipeline — Run stages

- **Files:** `src/loop_orchestrator/pipeline.py` (~1200 lines), `review.py`, `e2e.py`, `planning.py`,
  `secrets.py`, `jsonextract.py`
- **Tests:** `tests/test_pipeline_*.py`, `test_review.py`, `test_e2e.py`, `test_planning.py`
- **Related:** [[concepts/run-lifecycle]] · [[concepts/publication]] · [[concepts/agent-steering]]

## Purpose

`Pipeline` is the single place where a Run goes through its stages. `process()` drives an execution
Run, `process_planning()` a planning Run. Everything else in the file is stages and support machinery.

## Route through the code

| Method | What it does |
|---|---|
| `_prepare` | deletes apps of this PR's previous Runs, reads `.loop.yml` from the head branch, creates app+sandbox, uploads secrets |
| `_write_secrets` | `.loop/secrets.env` + `.loop/.gitignore` — [[concepts/secrets-delivery]] |
| `_execute` | the executor task: monotonic deadline, rate-limit pauses, transient resumes |
| `_review` / `_finish_review` | review + fix loop (capped by `LOOP_REVIEW_MAX_FIX_ITERATIONS`) |
| `_e2e` / `_finish_e2e` / `_send_e2e_videos` | Playwright scenarios, verdict, videos to Telegram |
| `_stage` → `_publish_ff` | two-phase publication — [[concepts/publication]] |
| `_start_preview` / `_notify_awaiting` / `expire_preview` | the pause with its preview link, and its expiry |
| `_planning` / `_prepare_planning` / `_publish_plan` / `_report_planning` | planner ⇄ advisor → PR |
| `sync_branch_with_base` | background conflict-resolver agent |
| `_submit_resumable` / `_drain_stale_task` / `_ensure_awake` / `_poll_wait` / `_sleep_awake` / `_task_status` | resilience — [[concepts/resilience]] |
| `_refresh_card` / `_report_success` / `_set_verdict_label` / `fail` | reporting |

## Invariants

- **The stage deadline is monotonic.** Rate-limit pauses do not eat into it — otherwise the execute
  timeout would expire on waiting rather than on work (`ExecutionTimeout`, a phase-1 review finding).
- **Polling is never a bare `sleep`.** Every wait goes through `_poll_wait`/`_sleep_awake`, which
  refresh the keepalive: without that the sandbox dies at minute 35.
- **Videos go out before `delete_app`.** Once the app is deleted the files/export API returns 404 for
  the whole workspace. The order in the code matters.
- **Review and e2e never block code delivery.** A rejection or an escalation → publication with the
  `loop:needs-review` label, not lost work.
- **A PR wears exactly one verdict label.** `_set_verdict_label` removes the previous one (`bd66a83`,
  `031f829`).
- **A failed secrets write is fatal** for the stage.

## Gotchas

- A greedy `\{.*\}` in the verdict parsers broke on prose preceding the JSON → all parsers now go
  through `jsonextract.find_json_object` (`9dc7727`).
- A `409` on submit means "the sandbox is not ready" **or** "someone else's task is stuck in it";
  retry until the deadline plus `_drain_stale_task` (`f77959c`, `c5a3a2f`).
- The agent's summary lives in `agent_message_final` on a single GET and in `agent_message` on the
  list endpoint — read both, otherwise the report arrives as "(no summary)".
- `revise` releases the temporary branch only **after** a successful submit — otherwise a failed
  revise would leave the Run with no published code.
- `e2e.services` in `.loop.yml` is still "not supported yet" — the stack is brought up by a script
  from `run:`, and that is the working path (the two-repo smoke test of 2026-08-04 brought up backend
  and frontend in a single sandbox).

## Connections

Called by `Worker`; talks to `GitHubClient`, `SandboxdClient`, `TelegramNotifier`; state changes go
through `state_machine.transition`.
