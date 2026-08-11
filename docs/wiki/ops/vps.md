# Ops: VPS — what lives there and how it bites

Step-by-step install — [`docs/deploy.md`](../../deploy.md). This page holds only the operational
knowledge earned by running the thing.

> **Placeholders.** `<vps-ip>`, `loop.example.com`, `<owner>`, `<org>` and the `<*-repo>` names stand
> in for the real values of one particular deployment. The repository never carries them: they live
> in `~/loop/.env` and `~/.sandboxd/src/.env` on the host itself
> ([[decisions/0011-no-environment-specifics-in-the-repo]]).

## Host

- `deploy@<vps-ip>`, Ubuntu 24.04. **4 vCPU / 16 GB / 4 GB swap**, `LOOP_MAX_CONCURRENT_RUNS=3`
  ([[decisions/0012-one-bigger-host-over-a-multi-host-pool]], moved 2026-08-06).
- The **previous** host was 2 cores / 8 GB / no swap and that was a design constraint, not a detail:
  three parallel Runs drove load average to 19, dockerd stopped answering the embedded resolver at
  127.0.0.11 in time, and the runs died on `Temporary failure in name resolution` (incident
  2026-08-05, [[concepts/resilience]]). It still runs an unrelated service of the owner's; the loop
  stack there is stopped, its sandboxd left in place.
- **Root SSH is disabled on the old host, sudo for `deploy` asks for a password.** Everything was
  installed and gets fixed without root.

## What a Run actually costs (measured 2026-08-06)

`docker stats` during two live Runs, both mid-stage:

| Container | CPU | RAM |
|---|---|---|
| sandbox of Run A | 98.6% | 3.50 GiB |
| sandbox of Run B | 97.2% | 3.02 GiB |
| orchestrator + sandboxd + traefik + caddy + console | **0.22% together** | **~270 MiB together** |

Two numbers worth remembering:

- **One Run = one whole core + ~3.5 GB.** Not an average with spikes — a sandbox running a build, a
  test suite or Playwright simply pins a core for as long as the stage lasts.
- **The control plane is free.** Everything that is not a sandbox costs a fifth of a percent of one
  core. Sizing this host is therefore purely a question of how many Runs must fit.

Sizing rule that follows: **N parallel Runs need `(N+1)` vCPU and `(3.5·N + 2)` GB** — the `+1` core
and `+2` GB are the reserve without which dockerd starves and the DNS incident repeats. So 2/8 holds
one Run comfortably and two at the wall (which is where the failures came from), 4/16 holds three,
8/32 holds six or seven.

Note that sandboxd imposes **no CPU ceiling of its own** and that host swap never reaches a sandbox —
both are properties of its hardcoded container spec, see [[concepts/sandboxd-platform]].

**The working way to edit system files without root:** `deploy` is in the `docker` group and the
daemon runs as root —
`docker run --rm --user 0 -v /etc/cron.d:/mnt/crond -v /home/deploy:/mnt/home loop-sandbox:latest bash -c '…'`.
That is exactly how cron got fixed (below).

## Moving the whole thing to another host

Done on 2026-08-06 with roughly 40 minutes of downtime, most of it image builds. The knowledge worth
keeping:

**Getting in.** Hostinger applies an SSH key attached through its API **only at provisioning** —
`VPS_attachPublicKeyV1` against a running machine registers the key and changes nothing on disk. On a
box with no other access the sequence is `VPS_createPublicKeyV1` → `VPS_attachPublicKeyV1` →
`VPS_recreateVirtualMachineV1`; the recreate wipes the machine, so it is only free on a fresh one.

**What actually has to move** (all of it small — the copy takes seconds, the image builds take the
rest):

| Path | Why |
|---|---|
| `~/.sandboxd/data/agent-auth/claude-code/.claude/.credentials.json` | **the Claude subscription's OAuth** — carry this and nobody has to redo the browser flow |
| `~/.sandboxd/data/state/` | the sandboxd DB: API keys and git credentials, so `LOOP_SANDBOXD_API_KEY` and `LOOP_GIT_CREDENTIAL_ID` in `.env` stay valid |
| `~/.sandboxd/data/secrets.key` | without it the stored git credential cannot be decrypted |
| `~/.sandboxd/src/` | sources, `.env`, compose, traefik config — control-plane and console are built from here |
| `~/loop/` | `.env`, `data/loop.db`, `secrets/*.env`; the code is overwritten by the next deploy anyway |
| `~/.ssh/authorized_keys` of `deploy` | carries the CI deploy key, so only `DEPLOY_HOST`/`DEPLOY_KNOWN_HOSTS` need changing |

Because `SANDBOXD_DATA_DIR` is an absolute path under `/home/deploy`, an identical username on the new
host means **no config edits at all** — the migrated `.env` files work as they are.

**Three things that bite:**

- Half of `~/.sandboxd/data` is root-owned and `deploy` cannot `sudo`. Read it through a root
  container: `docker run --rm --user 0 -v ~/.sandboxd/data:/mnt:ro --entrypoint tar loop-sandbox:latest -czf - -C /mnt agent-auth secrets.key state`.
- The console is behind a compose profile — `docker compose --profile console up -d --build`, or it
  silently never builds.
- The sandbox image is rebuilt from a Dockerfile that pulls *latest* node/claude/codex, so the new
  `loop-sandbox` is not byte-identical to the old one (6.48 GB against 4.53 GB). Re-verify the skills
  in `/opt/sandbox-skel/.claude/skills` after a rebuild.

**Cutover order** (the loop has no active runs at this point, which is what makes it safe): stop the
loop stack on the old host → copy the now-quiesced `loop.db` → bring the new stack up → flip DNS
(`@` and `*.preview`, TTL 300) → update `DEPLOY_HOST` and `DEPLOY_KNOWN_HOSTS`. The webhook URLs on
the target repositories and the Telegram webhook are **domain names**, so the DNS flip moves both and
nothing has to be re-registered. Caddy takes a couple of minutes to obtain its certificate over
TLS-ALPN once DNS resolves to the new box.

Stopping the old orchestrator first is not optional: its scheduler polls GitHub directly, so two live
orchestrators would both pick up `loop:ready` issues regardless of where DNS points.

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

**The fourth thing, learned the hard way on 2026-08-08:** the table above moves *data*, and the host
fixes stay behind. `/etc/cron.d/docker-image-prune` was recreated on the new box by the platform
installer with the original `-af`, the anchor containers were never recreated, and two nights later
the images were gone again (below). After any host move, replay the host-level fixes as well —
they are not in `~/.sandboxd/data`.

## Incident: cron ate the images (2026-08-04, again on 2026-08-08)

`loop-sandbox:latest` and `sandboxd-base:0.3.0` vanished from the VPS. The culprit —
`/etc/cron.d/docker-image-prune`: daily at 00:04, `docker image prune -af --filter until=24h`.
Sandbox seeding uses images **ephemerally** (`docker run --rm`), so between runs they count as
unused.

**Fixed:** `-af` → `-f` (only dangling junk gets cleaned), backup of the original at
`~/cron-docker-image-prune.bak`. The second line of defence stays: anchor stopped containers
`keep-loop-sandbox` / `keep-sandboxd-base` (`docker create --name keep-… <image> true`) — the image
counts as in use, prune leaves it alone.

**It happened again on 2026-08-08, on the new host, in exactly the same way** — the move carried the
data and not the fix (above). The prune ran at 03:35 UTC; the first Run after it (a planning Run on
`<frontend-repo>`) failed at sandbox creation with

```
create: aborting … seed: docker run --rm … loop-sandbox:latest … exit status 125:
Unable to find image 'loop-sandbox:latest' locally
pull access denied for loop-sandbox, repository does not exist
```

and then sat there, because nothing downstream treats a dead sandbox as dead
([[concepts/resilience]] §6). Recovery, ~25 minutes and no data lost:

```bash
cd ~/.sandboxd/src && SANDBOXD_IMAGE=sandboxd-base:0.3.0 bash image/build.sh 0.3.0
cd ~/loop && docker build --build-arg BASE_IMAGE=sandboxd-base:0.3.0 -t loop-sandbox:latest deploy/sandbox-image
docker run --rm --entrypoint sh loop-sandbox:latest -c 'ls /opt/sandbox-skel/.claude/skills'  # verify
docker create --name keep-loop-sandbox loop-sandbox:latest true
docker create --name keep-sandboxd-base sandboxd-base:0.3.0 true
```

sandboxd needs no restart — it resolves `SANDBOXD_IMAGE` per sandbox creation. The skills check is not
optional: the rebuild pulls *latest* node/claude, and the image is what puts `writing-specs`,
`writing-plans` and `playwright-cli` in front of an agent ([[ops/sandbox-image]]).

## Routine checks

- Live sandboxes after a failed Run: the fail path does not always reach `delete_app` — an orphaned
  sandbox keeps burning CPU and subscription quota. Look for them and clean up by hand. Ask sandboxd
  (`GET /v1/apps`), **not** `docker ps --filter name=s-`: the filter matches a substring, so any
  container with `s-` anywhere in its name counts as a sandbox.
- Sandboxes of Runs paused in `awaiting_approval` are **not** counted by `LOOP_MAX_CONCURRENT_RUNS`:
  the pipeline coroutine releases its consumer slot at the pause, while the sandbox and its dev server
  stay up for `LOOP_PREVIEW_TTL_MINUTES` (120). The real number of live sandboxes can therefore exceed
  the cap — see the sleep-on-pause plan in [[decisions/0012-one-bigger-host-over-a-multi-host-pool]].
- A probe can run straight from the host: `curl -H "Authorization: Bearer $KEY" http://127.0.0.1:9090/v1/...`
  works, no need to go through the orchestrator container ([[concepts/sandboxd-platform]]).
- The orchestrator's `/healthz` — the deploy workflow checks the same endpoint.

## Links

[[ops/deploy-and-ci]] · [[ops/sandbox-image]] · [[concepts/resilience]] · [[concepts/sandboxd-platform]]
