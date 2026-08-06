# Ops: repository, CI and auto-deploy

The project lives in the private **`<owner>/claude-loop-swe`**, default branch `master`.

## Pipeline

- `.github/workflows/ci.yml` — pytest; on PRs and pushes to every branch **except** `master`, plus
  `workflow_call`.
- `.github/workflows/deploy.yml` — push to `master` → calls `ci` as a reusable workflow → tar over
  ssh into `~/loop` → `docker compose up -d --build` → waits for a 200 from `/healthz`; on failure
  it dumps the container logs. `concurrency: deploy-vps` serialises runs.

Repository secrets: `DEPLOY_SSH_KEY` (a separate ed25519 key created specifically for the runner;
the public half is in `~/.ssh/authorized_keys` of the `deploy` user), `DEPLOY_KNOWN_HOSTS` (the host
key is pinned, `StrictHostKeyChecking` is never disabled), `DEPLOY_HOST`, `DEPLOY_USER`.

Before delivery the workflow probes the ssh connection with retries (8×20 s): twice on 2026-08-05
GitHub runners could not reach port 22 (transient, a re-run went through) — `8780aa8`.

## Deliberate limitations

- Deploy ships an **explicit list** of paths (`src pyproject.toml Dockerfile docker-compose.yml
  Caddyfile deploy`) and **deletes nothing** — it's tar, not rsync. That's by design: `~/loop` is not
  a checkout, it holds `.env`, the `data/` database and `secrets/`.
- **The sandbox image is not rebuilt** by deploy (4.5 GB, shared with sandboxd) — [[ops/sandbox-image]].
- **There is deliberately no linter in CI** — the repository carries no ruff/mypy configuration.
- Actions are pinned to v7: v4/v5 pull in Node 20 and GitHub annotates every run about it.

## The manual path (historical note)

Until 2026-08-04 deploy was `git archive master -- src … | ssh deploy@… "tar -x -C ~/loop"` +
`docker compose up -d --build`. The trick still works for emergency delivery of a single file.

## Tests

`python -m pytest tests -v` — pytest with `asyncio_mode = "auto"`, HTTP is mocked with respx, the
webhook goes through `httpx.ASGITransport` (lifespan does not start then, so no real clients are
created in tests). As of 2026-08-05 the suite has ~373 tests; get the current number from a run, not
from here.

## Links

[[ops/vps]] · [[ops/target-repos]] · CLAUDE.md §Commands
