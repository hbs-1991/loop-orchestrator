# Component: external-system clients

- **Files:** `src/loop_orchestrator/clients/` — `github.py`, `sandboxd.py`, `telegram.py`,
  `tg_card.py`, `tg_topics.py`, `tg_format.py`, `retry.py`
- **Tests:** `tests/test_github_client.py`, `test_sandboxd_client.py`, `test_telegram.py`,
  `test_tg_card.py`, `test_tg_topics.py`, `test_tg_format.py`, `test_retry.py`
- **Related:** [[concepts/sandboxd-platform]] · [[concepts/publication]]

A shared convention: every client accepts an optional `httpx.AsyncClient` (for respx in tests), and
transient errors go through `retry.with_retries` (3 attempts, 5xx and transport).

## GitHubClient

Labels (`LOOP_LABELS`), comments, files, branches, PRs, issues, check runs, `blocked_by`.

- `fast_forward` — `force: false` by construction: a ff that does not apply means the PR branch has
  moved ahead, and that is for a human to sort out.
- `merge_pr` — a squash with the title `pr_title (#N)`.
- `list_check_runs` + `required_checks` — the merge gate; **an empty list means "CI has not started
  yet"**, not "this repository has no checks" (`35d1330`).
- `update_pr_branch` — for `behind` with a protected base.

## SandboxdClient

A thin wrapper over the platform API; all of its weirdness is described in
[[concepts/sandboxd-platform]]. Worth remembering separately:

- `keepalive` hits the **internal** surface `POST /sandbox/{id}/keepalive` (no `/v1`) and swallows
  errors best-effort;
- `exec_cmd` runs a command **without the agent** — same internal surface, `POST /sandbox/{id}/exec`.
  A non-zero `exit_code` is a normal answer, only transport/HTTP failures raise. sandboxd passes
  neither `-u` nor `-w` to `docker exec`, so `cd` into the app directory yourself; it does bump
  `last_active_at`, so a loop built on it needs no keepalive;
- `start_sandbox` brings up a container the reaper stopped;
- `create_sandbox` on a 409 adopts the app's `current_sandbox_id`;
- `put_file` is the secrets delivery channel;
- `sanitize_git_config` removes the repo-local git config that blocked pushes (`f04c856`);
- `export_zip` is needed when a file is larger than 2 MiB — the files API will not serve it.

## Telegram

Bot API 10.x. Three layers:

- `TelegramNotifier` — sending. All notifications go through `sendRichMessage`
  (`{"rich_message": {"markdown": …}}`): Telegram natively renders tables, highlighted code,
  checkboxes and nested lists. **Tables render under no classic `parse_mode`** — only via rich
  messages. The degradation ladder: rich → `parse_mode=HTML` (the `tg_format.py` converter, tables →
  an aligned `<pre>`) → plain text.
- `tg_topics.TopicManager` — a forum topic per Run, fail-safe: a chat without topics → flat delivery.
- `tg_card` — the live checklist card (`sendMessage` silently + `editMessageText` after every
  transition, ✅⏳⬜➖⛔), title from `Run.pr_title`, times in `LOOP_TZ`.

Pushes are final-only (summary, videos, escalations, errors); progress lives in the card.

**Telegram gotchas:** in a private chat topics work only after the user enables "threaded mode" in
the bot settings; `closeForumTopic` is not supported there — the fail-safe swallows it, the topic
stays open but is renamed to ✅/⚠️/❌/🔀.

## Connections

Used by `Pipeline`, `Actions`, `Scheduler`, `Worker`.
