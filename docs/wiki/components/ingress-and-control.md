# Component: ingress and control — webhooks, buttons, actions

- **Files:** `src/loop_orchestrator/webhook.py`, `telegram_webhook.py`, `actions.py`, `main.py`
- **Tests:** `tests/test_webhook.py`, `test_telegram_webhook.py`, `test_actions.py`
- **Related:** [[concepts/run-lifecycle]] · [[concepts/publication]] · [[components/clients]]

## GitHub webhook (`/webhooks/github`)

HMAC signature → event filter. Two entry points:

- `pull_request.labeled` + `loop:run` → an execution Run (the "one active Run per PR" deduplication
  runs under an `asyncio.Lock` in `app.state.dedup_lock` — otherwise check-and-insert races against
  itself, a phase-1 review finding);
- `issues` (`labeled`/`unlabeled`/`closed`/`reopened`) → `_spawn_tick` in
  [[components/worker-and-scheduler]], passing the issue from the payload as `seed_issues` (working
  around the label indexing lag).

A lost `loop:run` webhook is cured by re-applying the label.

The GitHub webhook of each target repo is subscribed to `issues`, `issue_comment`, `pull_request` and
— since 2026-08-08 — **`check_run`**. `PATCH /repos/{repo}/hooks/{id}` replaces the event list wholesale,
so always send all four; a classic `repo` scope is enough to edit them.

## Telegram webhook (`/webhooks/telegram`)

`X-Telegram-Bot-Api-Secret-Token` is verified, then two kinds of updates:

- **callback_query** — buttons. The codes in `ACTION_CODES`: `ap` approve · `dc` discard · `cn` cancel ·
  `rs` restart · `mg` merge · `md` merge & deploy · `ub` update branch. Plus `ck`, which is **not** an
  action: it re-reads the gate and answers in the toast, so a stale keyboard can be refreshed on demand;
- **message** — a reply to the approval message means revise with the feedback text (the Run goes
  back to `executing` in the same sandbox).

Permissions come from `LOOP_TELEGRAM_ADMIN_IDS`. A non-admin gets a 200 with no effect (not a 403 —
so that Telegram does not retry). Actions run as background tasks, and references to them are held
in `_keep` so the garbage collector does not eat an unfinished coroutine.

`setWebhook` is called on application startup using `LOOP_PUBLIC_URL`.

## Actions

`approve` · `discard` · `revise` · `cancel` · `restart` · `merge` · `merge_deploy`.

- `merge`/`merge_deploy` go through `_merge_readiness` — the gate on CI, `behind`, and conflicts
  ([[concepts/publication]]). It returns a `Gate` named tuple; `done`/`total` on it exist only to
  label the button and never affect the decision.
- **The merge keyboard is an indicator, not a fixed pair of buttons.** Telegram has no disabled
  button, so instead of offering a press that would only be refused, `gate_kb` draws what the gate
  currently sees: `⏳ CI 2/3` while checks run, `🔴 CI red: <names>`, `⤴️ Update branch` when the
  branch is stale, `🔧 Resolve & merge` on a conflict, and the plain `🔀 Merge PR` / `🚀 Merge &
  Deploy` pair only when merging would actually be accepted. The message it edits is addressed
  through `run.tg_merge_message_id`.
  **Two triggers, on purpose.** The `check_run` delivery (`created`/`completed`/`rerequested`) is the
  prompt one: `_spawn_repaint` looks the PR up through `check_run.pull_requests[]` and repaints
  within seconds. The reaper's 60 s sweep (`refresh_merge_buttons_once`) is the safety net for a
  delivery GitHub dropped or one that landed while the orchestrator was restarting. Both funnel into
  `worker.repaint_merge_buttons`.
  The repaint is unconditional — a memo of "what was last drawn" would go stale the moment
  `_run_action` clears the keyboard after a press.
- `update_branch` (`ub`) is a **separate** action rather than a Merge press that happens to find the
  branch behind: the button promises one thing, so if the gate turned clean in between it refuses
  instead of merging something the user did not ask for.
- `merge_deploy` applies `LOOP_PROMOTE_LABEL` **before** the merge and removes it if the merge is
  rejected.
- `cancel` rescues the work in `loop/run-N` (`rescue_to_staging`) and leaves a comment; for a
  planning Run the comment goes to the issue, not to PR#0.
- `restart` opens **its own** Telegram topic — accepted by design.
- every revise cycle draws a **new** card with `revision N` in the header.

## Gotchas

- Cancelling a Run without a Telegram client: POST to `/webhooks/telegram` with callback
  `cn:<run_id>`, the secret header and an admin `from.id` — a working debugging trick.
- e2e videos are sent at the pause only when the approval message was **delivered**, otherwise at the
  finish (otherwise they got duplicated).

## Connections

`main.create_app` assembles the application and its lifespan: clients, `Worker`, `Scheduler`,
`Actions`, `setWebhook`, `/healthz`.
