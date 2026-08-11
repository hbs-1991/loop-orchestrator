# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**loop-orchestrator** is a Python service that turns the development loop into automation on top of the self-hosted sandbox platform [sandboxd](https://github.com/tastyeffectco/sandboxd): a GitHub webhook on the `loop:run` label → a fresh sandbox → Claude Code executes the plan from the PR → the code is published back to the PR branch → reports go to Telegram.

Key documents (source of truth, read before making changes):

- Spec: `docs/superpowers/specs/2026-07-31-loop-engineering-mvp-design.md` — every product decision, Locked Decisions, Open Questions.
- Implementation plan: `docs/superpowers/plans/2026-07-31-loop-orchestrator-mvp.md` — 14 TDD tasks with full code; executed task by task, checkboxes ticked right in the plan file.

## Project memory (LLM wiki — `docs/wiki/`)

`docs/wiki/` is a self-updating wiki following the LLM-Wiki pattern: a knowledge layer between the documents/code and the agent. **This is the project's "memory"**: what has actually been built, how the platform underneath us behaves, post-mortems of incidents, ops knowledge about the VPS and the target repositories.

- **Reading:** the `SessionStart` hook injects `docs/wiki/{index,overview}.md` plus the tail of `log.md` into every session's context on its own — no need to open them separately; drill down into specific pages instead.
- **Updating (mandatory):** new knowledge appeared (a feature was implemented, a decision was made, a gotcha was found, an incident was analysed, a smoke test or a probe was run) → `/wiki-ingest` (or the Ingest procedure from `docs/wiki/conventions.md` §4: a `components/`/`concepts/`/`ops/` page, `decisions/`, `overview.md`, `log.md`). The `Stop` hook will remind you if code/infrastructure/specs changed but the wiki did not. Health check — `/wiki-lint`.
- **Boundary (important):** the wiki **links to** the specs (`docs/superpowers/specs/`), the plans and this file, it does **not duplicate** them. Rules — `docs/wiki/conventions.md`.

## Commands

```bash
# environment (Windows: .venv/Scripts/pip, Linux: .venv/bin/pip)
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"

# all tests
python -m pytest tests -v

# a single file / a single test
python -m pytest tests/test_pipeline_execute.py -v
python -m pytest tests/test_db.py::test_create_and_get -v

# build the image
docker build -t loop-orchestrator .
```

Tests are pytest with `asyncio_mode = "auto"` (async tests need no decorators); HTTP is mocked through respx, the webhook through `httpx.ASGITransport` (lifespan does not run in that case — no real clients are created in tests).

**Deployment is automatic:** a push to `master` → `.github/workflows/deploy.yml` (first a `ci.yml` run, then a tar over ssh into `~/loop` on the VPS, `docker compose up -d --build`, a `/healthz` check). The manual `git archive | ssh` is no longer needed. Repository secrets: `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`, `DEPLOY_HOST`, `DEPLOY_USER`. The deploy does not delete files on the server (it is a tar, not an rsync) and **does not rebuild the sandbox image** — `deploy/sandbox-image` is built by hand on the VPS, instructions at the top of its Dockerfile.

## Architecture

A single FastAPI service, SQLite, an in-process asyncio worker (no Celery/Redis). The flow: `webhook.py` (HMAC, filter on `pull_request.labeled` + `loop:run`, dedup of "one active Run per PR") → `worker.py` (queue, 4 consumers, recovery after a restart) → `pipeline.py` (the prepare/execute/publish/report steps) → clients in `clients/` (github, sandboxd, telegram, shared retry).

Run states: `queued → preparing → executing → reviewing → e2e_testing → contracting → staging → awaiting_approval → publishing → reporting → done|failed|cancelled` (`reviewing`/`e2e_testing` are skipped by config; `contracting` is skipped when the Run has no issue or its issue blocks nobody; `awaiting_approval` is skipped when `.loop.yml` says `approval: never`); transitions are validated in `state_machine.py` and written to `run_events`. The review task runs in the same sandbox on the model from `LOOP_REVIEWER_MODEL` (default `claude-fable-5`), the verdict is JSON in the final message; a review failure does not block publication.

Planning is configurable per repository through the `planning:` section of `.loop.yml`, and every knob falls back to the platform setting it overrides: `planning.enabled` (read from the **default** branch by `loopconfig.planning_enabled`, because the scheduler decides before the issue branch exists — `false` leaves the issues in the backlog), `planning.model` → `LOOP_PLANNER_MODEL`, `planning.advisor.enabled` (`false` publishes the first plan without a review round), `planning.advisor.model` → `LOOP_ADVISOR_MODEL`, `planning.advisor.max_iterations` → `LOOP_PLAN_MAX_ITERATIONS`. The values are snapshotted onto the Run at `preparing`, so editing the config mid-Run does not change the rules that Run started under.

The e2e task runs in the same sandbox (model from `LOOP_E2E_MODEL`, defaulting to the executor's model): it writes Playwright scenarios from the spec via playwright-cli (the skill is baked into the sandbox image), the verdict is JSON in the final message; a failure → a fix loop (capped by `LOOP_E2E_MAX_FIX_ITERATIONS`), escalation does not block publication. Videos from `.loop/e2e/` go to Telegram (sandboxd files/export API; a file >2 MiB — only via export-zip).

Before publishing, a Run pauses (`awaiting_approval`): a push message with the summary, the e2e videos and a preview link (sandboxd's native preview, `GET /v1/sandboxes/{id}` → `preview.url`) goes into the thread. Control is via Telegram buttons (`telegram_webhook.py` → `actions.py`: approve/discard/cancel/restart/merge/merge_deploy — the last one puts the `LOOP_PROMOTE_LABEL` label on the PR before merging, default `promote:staging`, which the repository's promote workflow reacts to; a merge on a branch behind base does an `update-branch`, and on a conflict it starts a background resolver agent in a fresh sandbox with a temporary `GIT_SYNC_TOKEN` secret and retries the merge automatically) and revise replies to the approval message; permissions come from `LOOP_TELEGRAM_ADMIN_IDS`. Publication is still two-phase, but the pause splits it: the push to the temporary branch happens on `staging` (before the pause), the fast-forward of the PR branch on `publishing` (after approve). The pause sandbox lives for `LOOP_PREVIEW_TTL_MINUTES` (reaper in `worker.py`); its death does not prevent approve/merge — the code is already in the temporary branch.

Every Run outcome ends with a Telegram message. Each Run lives in its own forum topic (Bot API 10.0, fail-safe: a chat without topics → flat delivery) with a live progress card (`clients/tg_card.py`, `editMessageText`, silent updates); pushes are final only (summary, videos, escalations, errors). Titles come from the PR's `pr_title`; the timezone for card timestamps is `LOOP_TZ`.

The non-obvious constraints the system is built around (verified against the sandboxd sources — do not "improve" them):

- **A sandbox cannot do a git push.** Push is a host-side operation of the sandboxd control plane (`POST /v1/apps/{id}/git/push`), only into a **new** branch (the import branch and main/master are rejected), without force. Hence the two-phase publication: a push into the temporary branch `loop/run-<id>`, then the orchestrator fast-forwards the PR branch through the GitHub API (`force: false`); on a non-fast-forward the temporary branch is kept.
- **An app's git branch in sandboxd cannot be changed after creation** (`PATCH /v1/apps` only changes name/description/tags), and push cannot fetch/pull. Hence a fresh app + sandbox is created for every Run; apps from this PR's previous Runs are deleted during preparing.
- **The app config never reaches the agent at all** (verified by a probe and against the sources: `v1_app_config.go` keeps the values on the control plane, the broker its `access_policy` refers to has not been written yet; on top of that `cmd/runtimed/agentenv.go` strips from the agent's environment everything ending in `_TOKEN`/`_PASSWORD`/`_KEY`/`_SECRET`). Hence project secrets (per-repo files `secrets/<owner>__<repo>.env` on the server) travel into the sandbox **as a file**, `.loop/secrets.env`, via `PUT /v1/sandboxes/{id}/files`, with a `.loop/.gitignore` containing `*` placed next to it (some repos commit `.loop/`), while the stage prompts name only the key names and the line `set -a; . .loop/secrets.env; set +a`. The values end up neither in the prompt nor in the Run record. The upload into the app config (`POST /v1/apps/{id}/config`) is still there — it is harmless and will come in handy once the broker exists.
- An agent crash on execute is not lost work: a best-effort publication of the commits already made is performed (`_publish_partial`).

## Conventions

- **English everywhere, documentation included.** Code, comments, agent prompts, PR comments, Telegram messages, label descriptions — and every document in the repository: specs, plans, the wiki (`docs/wiki/`), READMEs, skills, hook texts, `.env.example`. A new document is written in English from the first line; never "in Russian for now, translate later". The only Russian left is the live conversation with the user. Rationale: the project is headed for open source, and a repository that documents itself in a language its readers do not share is closed in practice ([[decisions/0010-documentation-in-english]] in the wiki).
- **No environment specifics in documents.** Host addresses, domains, GitHub accounts and org names, target repository names, ids and absolute local paths are written as placeholders — `<vps-ip>`, `loop.example.com`, `<owner>`, `<org>`, `<backend-repo>`. If a value would differ for another reader, it is a placeholder. Real values live on the host in `~/loop/.env` and in the repository secrets; `.env.example` shows the format only ([[decisions/0011-no-environment-specifics-in-the-repo]] in the wiki).
- Settings go through `Settings` only (pydantic-settings, prefix `LOOP_`).
- HTTP clients accept an optional `httpx.AsyncClient` (for respx/ASGI in tests); transient errors (5xx, transport) go through `clients/retry.with_retries`, 3 attempts.
- The `.loop.yml` format, the `loop:*` label names, the Run states and the publication scheme are Locked Decisions of the spec; change them only by updating the spec.
- The project workflow: brainstorming → spec → writing-plans → executing the plan (the `.claude/skills/parallel-plan-execution` skill — fanning the plan's tasks out across subagents with disjoint file sets).
