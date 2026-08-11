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
- The production repos started with **no branch protection** — the reason the CI gate lives in the
  orchestrator at all ([[decisions/0006-merge-gate-and-conflict-resolver]]). A ruleset exists now;
  as of 2026-08-08 the backend requires `gates`, `tests-selective` and `image`. We never hardcode
  that list — `required_checks` reads `/repos/{repo}/rules/branches/{base}` on every press, which is
  why the CI rebuild below cost us no change at all. The strict "up to date" rule is **off** (see
  [[concepts/publication]] on why we compute `behind_by` ourselves), and the bypass list stays empty,
  otherwise the owner's PAT would sidestep the rule.
- **CI runs on a self-hosted pool since 2026-08-07** (backend ADR 0076). The org exhausted its
  GitHub-hosted minutes, so every job in both repos moved to `runs-on: ${{ vars.CI_RUNNER }}` →
  label `ssc-build` → two ephemeral runners on the **old** 2-core VPS the loop stack vacated on
  2026-08-06. Three consequences for us:
  - **Self-hosted minutes are not billed**, which is why the buttons work again despite the payment
    block on Actions.
  - **Two slots, shared by both repos.** A PR now runs `gates` + `tests-selective` + `image`
    (docker build + Trivy) on two cores, so `checks_pending` lasts materially longer than it did on
    GitHub-hosted runners, and `update-branch` after a `behind_by > 0` costs a full re-run of all
    three. `behind_by == 0` short-circuiting matters more here than it did.
  - **The runner box is a single point of failure for our buttons.** Both runners down or busy ⇒
    every merge answers "checks are still running" forever. Diagnose it as
    `gh api orgs/<org>/actions/runners`, not as a loop bug; the owner's escape hatch is setting the
    org variable `CI_RUNNER` to `ubuntu-latest`.
  - `tests-full` was removed for good (~25 min on two cores) and the coverage floor went with it.
- Promotion to staging is picked up by the `promote-staging.yml` workflow **only in the backend**; in
  the frontend the `Merge & Deploy` button is harmless but triggers no deploy.

## Filing a task

Through the user-level `create-issue` skill (`~/.claude/skills/create-issue/SKILL.md`):
brainstorm → issue body template → approval → `gh issue create --body-file` → lane label → native
`blocked_by` → **`loop:ready` last**.

## Links

[[components/worker-and-scheduler]] · [[concepts/publication]] · [[concepts/secrets-delivery]] ·
[[ops/vps]]
