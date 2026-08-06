# Ops: target repositories under loop's control

State as of 2026-08-05. What a repository needs to take part in the loop: the `loop:*` labels, a
webhook on `pull_request` + `issues` + `issue_comment`, an entry in `LOOP_BACKLOG_REPOS`, a `.loop.yml`
file, and — if `required_env` is non-empty — a secrets file `~/loop/secrets/<owner>__<repo>.env`.

> **Placeholders.** `<org>`, `<owner>`, `<backend-repo>` and `<frontend-repo>` stand in for the real
> names of one particular deployment; the repository does not carry them
> ([[decisions/0011-no-environment-specifics-in-the-repo]]). What matters here is the shape of the
> configuration and the gotchas, not whose repositories these were.

## Smoke-test repositories

- **`<org>/loop-smoke-test`** — public, the main proving ground (PR#1–#22, Run#1–#30). Every
  phase was exercised here. Worth remembering: it is **public**, so the "the token reached the agent"
  smoke test on it was a false positive ([[concepts/secrets-delivery]]).
- **`<org>/loop-frontend-smoke`** — the second repository for the two-repo scenario: a
  stdlib-Python frontend skeleton, `run: sh scripts/run_stack.sh` (clones the backend using
  `GH_TOKEN`, brings up backend :8001 and frontend :3000), secret `GH_TOKEN`.

**The two-repo smoke test (2026-08-04) passed end to end:** a backend issue blocked a frontend issue
through the native cross-repo dependency → closing the backend one unblocked the frontend (picked up
by the poller, not the webhook) → the frontend planner cloned the backend itself and read the real
API contract → e2e brought up **both services in a single sandbox**, 8 Playwright scenarios green.

## Production repositories

- **`<org>/<backend-repo>`** — `setup: uv sync --frozen`;
  `test` = ruff + format + mypy --strict + lint-imports + `pytest tests/unit tests/architecture
  tests/contracts tests/integration` (~3 min). `tests/feature`/`tests/modules`/`tests/data_migration`
  are excluded **on time** (>25 min), not on dependencies — conftest substitutes dummy values.
  No `run:`/`e2e:`: the API needs Postgres + Redis + Soketi, none of which exist in the sandbox (nor
  does docker with sudo).
- **`<org>/<frontend-repo>`** — pnpm, `test: pnpm lint && pnpm test:run`, `run: pnpm dev`,
  e2e enabled. `setup` runs `pnpm exec playwright install chromium` (version drift against the
  pre-baked browser); `PLAYWRIGHT_REUSE_SERVER: "1"` in `e2e.env` is **mandatory**:
  `playwright.config.ts` deliberately refuses to attach to someone else's server, and the e2e agent
  brings up `pnpm dev` on its own.
- `<admin-repo>` did not make the scope (owner is `<owner>`, not `<org>`) — the user's
  decision.

## Provisioning gotchas

- **Always branch from `origin/main`, never from local main.** The local mains of both production
  repos were ahead of origin by the user's unpushed commits, and the branch dragged them into the PR.
  Fix: rebuild the branch from `origin/main` in a separate worktree + `push --force-with-lease`.
- **The PAT has no Administration scope** — you cannot create a repository with it (the user does
  that), and a new repository must be **added to the fine-grained PAT's list**, otherwise 404.
- `gh api` for `blocked_by` requires `-F issue_id=…` (a number); `-f` sends a string.
- The production repos have **no branch protection** — which is exactly why the CI gate lives in the
  orchestrator ([[decisions/0006-merge-gate-and-conflict-resolver]]). The user got a rulesets guide
  (required checks: backend `ci`; frontend `Lint`/`Unit & Integration Tests`/`Build`; deploy-side
  checks are **not** required — they don't run on PRs; the bypass list stays empty, otherwise the
  owner's PAT would sidestep the rule).
- Promotion to staging is picked up by the `promote-staging.yml` workflow **only in the backend**; in
  the frontend the `Merge & Deploy` button is harmless but triggers no deploy.

## Filing a task

Through the user-level `create-issue` skill (`~/.claude/skills/create-issue/SKILL.md`):
brainstorm → issue body template → approval → `gh issue create --body-file` → lane label → native
`blocked_by` → **`loop:ready` last**.

## Links

[[components/worker-and-scheduler]] · [[concepts/publication]] · [[concepts/secrets-delivery]] ·
[[ops/vps]]
