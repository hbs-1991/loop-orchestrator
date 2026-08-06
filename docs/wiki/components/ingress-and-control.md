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

## Telegram webhook (`/webhooks/telegram`)

`X-Telegram-Bot-Api-Secret-Token` is verified, then two kinds of updates:

- **callback_query** — buttons. The codes in `ACTION_CODES`: `ap` approve · `dc` discard · `cn` cancel ·
  `rs` restart · `mg` merge · `md` merge & deploy;
- **message** — a reply to the approval message means revise with the feedback text (the Run goes
  back to `executing` in the same sandbox).

Permissions come from `LOOP_TELEGRAM_ADMIN_IDS`. A non-admin gets a 200 with no effect (not a 403 —
so that Telegram does not retry). Actions run as background tasks, and references to them are held
in `_keep` so the garbage collector does not eat an unfinished coroutine.

`setWebhook` is called on application startup using `LOOP_PUBLIC_URL`.

## Actions

`approve` · `discard` · `revise` · `cancel` · `restart` · `merge` · `merge_deploy`.

- `merge`/`merge_deploy` go through `_merge_readiness` — the gate on CI, `behind`, and conflicts
  ([[concepts/publication]]).
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
