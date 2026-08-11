# 0015 — Sleep the paused sandbox; the preview wakes it on demand

- **Status:** accepted
- **Date:** 2026-08-08
- **Related:** [[components/pipeline]] · [[concepts/sandboxd-platform]] · [[concepts/resilience]] ·
  [[decisions/0012-one-bigger-host-over-a-multi-host-pool]] · [[decisions/0003-keepalive-against-idle-reaper]]

## Context

A Run pausing in `awaiting_approval` released its worker slot but not its sandbox: the pipeline
explicitly held the sandbox awake for the whole `LOOP_PREVIEW_TTL_MINUTES` (120) so the preview link
would survive sandboxd's idle reaper. One paused Run therefore parked ~3.5 GB and a running dev server
**outside** `LOOP_MAX_CONCURRENT_RUNS`, which made the cap a statement about the worker rather than
about the host. At a cap of 3 the real number of live sandboxes could be 5–6, and the measured sizing
rule (`(N+1)` vCPU, `(3.5·N + 2)` GB — [[ops/vps]]) then describes a host that does not exist.

The blocker was never the stop; it was the comeback. `_start_preview` launches the app's `run:` command
with `nohup` through the exec endpoint, and such a process dies with its container and nothing brings
it back — so a stopped sandbox meant a preview link that answered 502 forever.

This is not in any spec: the specs fix the pause, the buttons and the TTL, not how the sandbox spends
the pause.

## Decision

**Declare the preview server in the platform's own runtime manifest, then stop the sandbox for the
duration of the pause.**

`build_preview_manifest` writes `sandbox.yaml` (`version: 1`, `web.command`, `web.port`,
`health_path`) next to the app, so runtimed owns the server: it starts it on every container start.
Traefik's file-provider catch-all `sandbox-wake` (priority 1, below the per-sandbox routers at 100)
forwards a hit on a stopped sandbox's preview host to sandboxd, which starts the container and proxies
the request. The pause therefore costs nothing until somebody actually looks at it.

Three guards, all verified by the probe of 2026-08-08:

- **The manifest is written only after the exec-started server has answered its port.** A manifest
  naming a command that does not work replaces a working default preview with none at all.
- **A repository that tracks its own `sandbox.yaml` is never overwritten** — that file belongs to the
  app, and rewriting it would ride into the next revise commit and the PR diff. Such a Run keeps the
  old awake pause. When the path is free, our file goes into `.git/info/exclude` (per-clone, never
  committed) so `git add -A` by the revise agent cannot pick it up.
- **No manifest, no sleep.** `_start_preview` returns whether the pause is sleepable; when it is not,
  the keepalive path of [[decisions/0003-keepalive-against-idle-reaper]] stays exactly as it was.

`Actions.revise` wakes the sandbox before submitting (`start_sandbox`, idempotent and synchronous), and
the approval message says the preview is asleep — the first open takes ~10 s and can answer 502 while
the server binds.

## Alternatives

- **Keep holding the sandbox awake and buy a bigger host.** Not either/or: the host still has to be
  sized for its *active* Runs. But paying for two hours of an idle dev server per Run is the wrong half
  of the bill to grow.
- **Route the preview through the orchestrator** (our own endpoint wakes the sandbox, then redirects).
  Rejected: it puts our service in the data path of every preview request and duplicates a wake path
  the platform already implements.
- **Restart the exec-started server after each wake.** There is no hook to run it on: nothing tells the
  orchestrator that a wake happened, and polling for it would be the keepalive again under a new name.
- **Snapshot the sandbox and recreate it on approve.** Heavier and lossier than a stop: the snapshot API
  requires a stopped sandbox anyway, and recreation loses the agent's session that a revise wants.

## Consequences

- A paused Run holds a workspace on disk and **no memory**. The concurrency cap now means what it says,
  and the sizing rule applies to active Runs only.
- **The first preview open after a sleep can be a 502.** Measured: 8–14 s to wake, the next hit is a
  normal 200. The approval message states it; do not "fix" it by polling the link warm.
- `sandbox.yaml` is now a file the orchestrator may write into a target repository's working tree. It is
  excluded per-clone, never committed, and skipped entirely when the repo owns the name.
- Anything that touches a paused sandbox must wake it first. Today that is `revise`; approve, merge and
  discard need no sandbox at all (publication is a control-plane and GitHub-API affair), and
  `expire_preview` deletes the app either way.
- Watch the run_events line `sandbox stopped for the pause`: its absence on a paused Run means the
  manifest path declined, which is the case worth investigating rather than the sleep itself.
