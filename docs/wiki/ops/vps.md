# Ops: VPS — what lives there and how it bites

Step-by-step install — [`docs/deploy.md`](../../deploy.md). This page holds only the operational
knowledge earned by running the thing.

> **Placeholders.** `<vps-ip>`, `loop.example.com`, `<owner>`, `<org>` and the `<*-repo>` names stand
> in for the real values of one particular deployment. The repository never carries them: they live
> in `~/loop/.env` and `~/.sandboxd/src/.env` on the host itself
> ([[decisions/0011-no-environment-specifics-in-the-repo]]).

## Host

- `deploy@<vps-ip>`, Ubuntu 24.04. Another service of the owner's shares the host — leave it alone.
- **2 cores / 8 GB / no swap.** Not a detail — a design constraint: three parallel Runs drove load
  average to 19, dockerd stopped answering the embedded resolver at 127.0.0.11 in time, and the runs
  died on `Temporary failure in name resolution` (incident 2026-08-05,
  [[concepts/resilience]]). Hence `LOOP_MAX_CONCURRENT_RUNS=2` — four sandboxes at ~2 GB each do not
  fit here.
- **Root SSH is disabled, sudo for `deploy` asks for a password.** Everything was installed and gets
  fixed without root.

**The working way to edit system files without root:** `deploy` is in the `docker` group and the
daemon runs as root —
`docker run --rm --user 0 -v /etc/cron.d:/mnt/crond -v /home/deploy:/mnt/home loop-sandbox:latest bash -c '…'`.
That is exactly how cron got fixed (below).

## Where things live

| What | Where |
|---|---|
| sandboxd (sources, also the answer to "how does it actually behave") | `~/.sandboxd/src` |
| sandboxd data | `~/.sandboxd/data` |
| sandboxd API | `127.0.0.1:9090` from outside, `http://sandboxd:9000` from inside the `sandboxd_net` network |
| orchestrator | `~/loop` (docker compose) — **not a git checkout** |
| live database | `~/loop/data/loop.db` |
| project secrets | `~/loop/secrets/<owner>__<repo>.env` |
| orchestrator env | `~/loop/.env` |

## Network and domains

- Webhook: `https://loop.example.com/webhooks/github`. TLS is terminated by **Caddy** on
  :443 in the `~/loop` compose (TLS-ALPN — port 80 is taken by sandboxd's traefik). Public 8000 is
  closed, only 127.0.0.1 is exposed.
- Sandbox previews: `PREVIEW_DOMAIN=loop.example.com`, `PREVIEW_TLS=false` in
  `~/.sandboxd/src/.env`; wildcard record `*.preview` → <vps-ip>. Links look like
  `http://s-<id>-3000.preview.loop.example.com` (HTTP, port 80 of sandboxd's traefik).
- DNS is managed through the Hostinger API (MCP `hostinger-dns`), token lives in the user's
  environment variable.

## Incident: cron ate the images (2026-08-04)

`loop-sandbox:latest` and `sandboxd-base:0.3.0` vanished from the VPS. The culprit —
`/etc/cron.d/docker-image-prune`: daily at 00:04, `docker image prune -af --filter until=24h`.
Sandbox seeding uses images **ephemerally** (`docker run --rm`), so between runs they count as
unused.

**Fixed:** `-af` → `-f` (only dangling junk gets cleaned), backup of the original at
`~/cron-docker-image-prune.bak`. The second line of defence stays: anchor stopped containers
`keep-loop-sandbox` / `keep-sandboxd-base` (`docker create --name keep-… <image> true`) — the image
counts as in use, prune leaves it alone.

## Routine checks

- Live sandboxes after a failed Run: the fail path does not always reach `delete_app` — an orphaned
  sandbox keeps burning CPU and subscription quota. Look for them and clean up by hand.
- `docker exec loop-loop-orchestrator-1 python /tmp/probe.py` — sandboxd behaviour probe
  ([[concepts/sandboxd-platform]]).
- The orchestrator's `/healthz` — the deploy workflow checks the same endpoint.

## Links

[[ops/deploy-and-ci]] · [[ops/sandbox-image]] · [[concepts/resilience]] · [[concepts/sandboxd-platform]]
