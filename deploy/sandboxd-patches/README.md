# Local patches to sandboxd

The platform underneath us is someone else's service, checked out on the VPS at
`~/.sandboxd/src` and built there. A patch in this directory is a change we needed and could not get
any other way — through configuration, through the API, or by changing our own code.

**They are not applied automatically.** A sandboxd upgrade (`git pull` in that checkout, or a
reinstall) drops every one of them, and the symptom is silent: the platform keeps working, just
without whatever the patch gave us. Re-apply after any upgrade.

```bash
cd ~/.sandboxd/src
git apply /path/to/0001-per-sandbox-resource-ceilings.patch
docker compose build sandboxd && docker compose up -d sandboxd
```

Each patch is written to be **inert by default**: with no configuration set it must reproduce
upstream behaviour exactly. That keeps the diff reviewable and makes it easy to hand upstream.

## 0001 — per-sandbox resource ceilings

**Why.** sandboxd hardcoded `CPUShares: 100`, `Memory: "10g"`, `MemorySwap: "10g"` as literals in two
places (the create path in `internal/api/handlers.go` and the recreate path in
`internal/sandboxspec/spec.go`) and had no `--cpus` support at all — `docker.RunSpec` simply had no
field for it. Three consequences: nothing stopped one sandbox from taking every core on the host
(which is how dockerd stopped answering its embedded resolver on 2026-08-05, killing two Runs); a
memory ceiling larger than the host's RAM is not a ceiling; and `MemorySwap == Memory` disables swap
inside the container, so host swap never reaches a sandbox.

**What it does.** Adds `CPUs` to `docker.RunSpec` (the package's own rule is "no magic defaults", so
it only learns to spell the flag), introduces `sandboxspec.LimitsFromEnv()` as the one place the
policy lives, and uses it from both paths. The environment variables are passed through
`docker-compose.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `SANDBOXD_CPUS` | *(empty)* | `--cpus`; empty means no ceiling — upstream behaviour |
| `SANDBOXD_MEMORY` | `10g` | `--memory` |
| `SANDBOXD_MEMORY_SWAP` | `10g` | `--memory-swap`; equal to `Memory` disables container swap |

**What we set** on a 4-core / 16 GB host running `LOOP_MAX_CONCURRENT_RUNS=3`:

```
SANDBOXD_CPUS=3
SANDBOXD_MEMORY=5g
SANDBOXD_MEMORY_SWAP=7g
```

A single Run may still take three cores — measured, a planner really does use them — but never the
fourth, so dockerd, traefik and the orchestrator always keep one. That is precisely what starved on
2026-08-05. `5g` sits well above the observed peak of ~3.5 GB, so a runaway Run is OOM-killed alone
instead of taking the host with it, and `7g` of `memory-swap` gives each sandbox 2 GB of swap where
it previously had none.

Verify after applying:

```bash
docker inspect s-<id> --format 'NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}} MemorySwap={{.HostConfig.MemorySwap}}'
# NanoCpus=3000000000 Memory=5368709120 MemorySwap=7516192768
```
