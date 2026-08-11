# Component: pipeline — Run stages

- **Files:** `src/loop_orchestrator/pipeline/` (a package — one module per stage, see the map below),
  `review.py`, `e2e.py`, `planning.py`, `contracts.py`, `secrets.py`, `jsonextract.py`
- **Tests:** `tests/test_pipeline_*.py`, `test_review.py`, `test_e2e.py`, `test_planning.py`,
  `test_contracts.py`
- **Related:** [[concepts/run-lifecycle]] · [[concepts/publication]] · [[concepts/agent-steering]] ·
  [[concepts/contract-handoff]]

## Purpose

`Pipeline` is the single place where a Run goes through its stages. `process()` drives an execution
Run, `process_planning()` a planning Run. Everything else in the package is stages and support
machinery.

## Route through the code

One module per stage; `Pipeline` is `core.Pipeline`, which inherits a mixin from each of them.
`pipeline/__init__.py` re-exports the old module's surface, so every caller and every test still
imports from `loop_orchestrator.pipeline`.

| Module | Method | What it does |
|---|---|---|
| `core` | `process` | the PR Run's state machine — the one place the stage order is written down |
| `prepare` | `_prepare` | deletes apps of this PR's previous Runs, reads `.loop.yml` from the head branch, creates app+sandbox, uploads secrets and upstream context |
| `prepare` | `_write_secrets` | `.loop/secrets.env` + `.loop/.gitignore` — [[concepts/secrets-delivery]] |
| `prepare` | `_write_context` | `.loop/context/<repo>/<path>` — the upstream sources this Run consumes |
| `execute` | `_execute` | the executor task: monotonic deadline, rate-limit pauses, transient resumes |
| `review_stage` | `_review` / `_finish_review` | review + fix loop (capped by `LOOP_REVIEW_MAX_FIX_ITERATIONS`) |
| `e2e_stage` | `_e2e` / `_finish_e2e` / `_send_e2e_videos` | Playwright scenarios, verdict, videos to Telegram |
| `contract_stage` | `_contracting` / `_finish_contract` / `_publish_contract_comment` | describes the interface built for the tasks this issue blocks — [[concepts/contract-handoff]] |
| `publish` | `_stage` → `_publish_ff` | two-phase publication — [[concepts/publication]] |
| `preview` | `_start_preview` / `_arm_preview_manifest` / `_preview_responds` / `_note_preview_failure` / `_notify_awaiting` / `_sleep_pause` / `expire_preview` | the pause with its preview link, its sleep, and its expiry |
| `planning_stage` | `process_planning` / `_planning` | planner ⇄ advisor → PR; both agents' models and the advisor itself are per-repo settings ([[components/storage-and-config]]). `_prepare_planning` is in `prepare`, `_publish_plan` in `publish`, `_report_planning` in `reporting` |
| `gitsync` | `sync_branch_with_base` | background conflict-resolver agent |
| `sandbox_tasks` | `_run_sandbox_task` / `_submit_resumable` / `_drain_stale_task` / `_ensure_awake` / `_poll_wait` / `_sleep_awake` / `_task_status` | one agent task, and surviving the platform around it — [[concepts/resilience]] |
| `reporting` | `_refresh_card` / `_report_success` / `_report_planning` / `_set_verdict_label` / `fail` | labels, PR comments, Telegram |
| `tracing_mixin` | `_trace_start` / `_trace_task` / `_emit_run_span` | spans for finished sessions — [[components/tracing]] |

Leaves, imported by the stages and importing nothing of the package: `errors` (the five exceptions),
`constants` (`MAX_TASK_TIMEOUT_S`, the failure markers and the `failure_blob`/`is_rate_limited`/
`is_transient` triple both polling loops classify with), `clock`.

## Invariants

- **The stage deadline is monotonic.** Rate-limit pauses do not eat into it — otherwise the execute
  timeout would expire on waiting rather than on work (`ExecutionTimeout`, a phase-1 review finding).
- **Polling is never a bare `sleep`.** Every wait goes through `_poll_wait`/`_sleep_awake`, which
  refresh the keepalive: without that the sandbox dies at minute 35.
- **Videos go out before `delete_app`.** Once the app is deleted the files/export API returns 404 for
  the whole workspace. The order in the code matters.
- **Review and e2e never block code delivery.** A rejection or an escalation → publication with the
  `loop:needs-review` label, not lost work.
- **Contracting never blocks code delivery either.** A missing blocking list, a timed-out task or an
  unparsable verdict sets `contract_status` and moves on to `staging`; the consumer's planner gate
  turns the missing contract into questions rather than into a guess ([[concepts/contract-handoff]]).
- **A failed context upload is not fatal.** `_write_context` swallows into `run_events`: the digest in
  `.loop/task.md` is already committed, so the failure degrades the context instead of losing it —
  unlike `_write_secrets`, where a silent absence would fail a stage far less legibly later.
- **A PR wears exactly one verdict label.** `_set_verdict_label` removes the previous one (`bd66a83`,
  `031f829`).
- **A failed secrets write is fatal** for the stage.
- **Mechanical work goes through `exec`, not through the agent.** The preview server is started by
  `build_preview_script` over `SandboxdClient.exec_cmd` — a shell script, not a prompt. An agent task
  would cost a model call and ~40 s to type a command the executor has already made runnable, and
  being necessarily a fresh session it would have to restate the command, the environment and where
  the credentials live every time. Reach for `submit_task` only where judgement is needed.
- **The preview URL is published only after the port answers.** `_preview_responds` polls the
  sandbox's own port through `exec`; if it never answers the link is withheld and
  `_note_preview_failure` keeps the tail of `.loop/preview.log` in `run_events` — the log dies with
  the app, the event does not. A link that greets a reviewer with a 502 is worse than no link.
- **The pause sleeps, and only when its preview can come back.** `_arm_preview_manifest` writes
  `sandbox.yaml` (verified *after* the exec-started server answered, never before, and never over a
  `sandbox.yaml` the repository tracks); only then does `_sleep_pause` stop the sandbox. When the
  manifest path declines, the old keepalive-for-the-whole-TTL behaviour stays
  ([[decisions/0015-sleep-the-paused-sandbox]]). The sleep happens **after** `_notify_awaiting`,
  because the e2e videos are read out of the workspace there.

## Gotchas

- **There are two polling loops, and that is deliberate.** `execute._execute` owns `run.task_id` (so
  a restart picks the same task back up), overruns as `ExecutionTimeout`, and resumes with a prompt
  naming the plan; `sandbox_tasks._run_sandbox_task_inner` owns nothing, overruns as `ReviewDeadline`
  and returns an extended deadline to its caller. Only the part that would silently drift — how a
  failed task is read — is shared, in `constants.failure_blob`/`is_rate_limited`/`is_transient`.
- **Time goes through `clock.monotonic()`, not a bound `monotonic`.** The stage modules import the
  `clock` module and call through it, so the test fake (`patch_clock` in `test_pipeline_execute.py`)
  is a single `monkeypatch.setattr` that reaches every stage. Binding `from .clock import monotonic`
  in a stage module would silently opt that stage out of the fake clock.
- A greedy `\{.*\}` in the verdict parsers broke on prose preceding the JSON → all parsers now go
  through `jsonextract.find_json_object` (`9dc7727`).
- A `409` on submit means "the sandbox is not ready" **or** "someone else's task is stuck in it"
  **or** "the sandbox is dead and will answer 409 forever"; retry until the deadline plus
  `_drain_stale_task` (`f77959c`, `c5a3a2f`), but ask `_sandbox_is_dead` first — the third case cost a
  Run three silent hours ([[concepts/resilience]] §6).
- **Every planner round opens a fresh session, `revise` included.** sandboxd resumes only *the most
  recent* session, and after round 0 that is the advisor's — `continue` would have handed the planner
  the reviewer's context rather than its own. `build_planner_revise_prompt` therefore restates the
  task file, both document paths and the commit rules ([[decisions/0013-one-session-per-stage]]).
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
