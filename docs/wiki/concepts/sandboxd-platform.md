# Concept: sandboxd as a platform — what was verified, not documented

The orchestrator is built on top of self-hosted [sandboxd](https://github.com/tastyeffectco/sandboxd) —
someone else's Go service running on the same VPS. Its actual behaviour **diverged from expectations
several times**, and nearly every such divergence cost us a failed Run. This page is a summary of what
has been verified by a probe or by reading the sources.

**Where the truth lives:** the platform sources sit on the VPS in `~/.sandboxd/src`. That is the first
place to ask "how does it actually work", ahead of any documentation.

**How to test a hypothesis cheaply (a working technique):** a one-off probe script on top of
`SandboxdClient` inside the orchestrator container —
`docker exec loop-loop-orchestrator-1 python /tmp/probe.py` with `sys.path.insert(0, "/app/src")`:
create_app → create_sandbox → submit_task with a JSON response schema → delete_app. Live runs are left
untouched.

## Constraints the system is built around

| Constraint | How it surfaced | Consequence in the code |
|---|---|---|
| **A sandbox cannot do `git push`** | push is a host-side control plane operation (`POST /v1/apps/{id}/git/push`), into a **new** branch only, no force; the import branch and main/master are rejected | two-phase publication — [[concepts/publication]] |
| **An app's branch cannot be changed after creation** | `PATCH /v1/apps` only changes name/description/tags, and push cannot fetch/pull | a fresh app + sandbox per Run; apps from previous Runs of this PR are deleted during `preparing` |
| **App config never reaches the agent at all** | probe 2026-08-05 + `internal/api/v1_app_config.go` (the broker described by `access_policy` was never written) + `cmd/runtimed/agentenv.go:85` strips from the env everything ending in `_TOKEN`/`_KEY`/`_SECRET`/`_PASSWORD`/`_CREDENTIALS`/`_APIKEY` and everything starting with `RUNTIMED_` | secrets travel as a file — [[concepts/secrets-delivery]], [[decisions/0002-secrets-as-file]] |
| **A hidden ~35-minute ceiling per Run** | the idle reaper (`internal/reaper/idle.go`) runs `docker stop` on any sandbox whose `last_active_at` is older than `SANDBOXD_IDLE_THRESHOLD_SECONDS=2100`; the async task API does **not** bump `last_active_at` | keepalive on every poll tick — [[decisions/0003-keepalive-against-idle-reaper]] |
| **The reaper stops the container, it does not delete it** | the workspace, uncommitted work and the agent session live on the volume | `POST /v1/sandboxes/{id}/start` brings the sandbox back to life (`SandboxdClient.start_sandbox`, `Pipeline._ensure_awake`) |
| **A stop/start cycle keeps the preview route** | probe 2026-08-06, below | a paused Run can sleep instead of holding a live dev server — [[ops/vps]] |
| **A hit on a stopped sandbox's preview host starts it** (traefik catch-all at priority 1 → sandboxd), and a server declared in `sandbox.yaml` is restarted by runtimed; an `exec`-started one is not | probe 2026-08-08, below | the pause sleeps by default — [[decisions/0015-sleep-the-paused-sandbox]] |
| **The sandbox home directory is not what is in the image** | sandboxd bind-mounts a per-sandbox loopback workspace over `/home/sandbox`; the home is seeded once from `/opt/sandbox-skel` (`control-plane/internal/loopback/loopback.go`) | skills are placed into `/opt/sandbox-skel/.claude/skills/` — [[decisions/0004-skills-into-sandbox-skel]] |
| **The Files API dies together with the app** | after `delete_app` the whole workspace returns 404 "no such directory" | file diagnostics and video export are only possible while the Run is alive; `_send_e2e_videos` must run **before** `delete_app` |

## Small but biting API details

- **Task summary.** A single `GET /v1/sandboxes/{id}/tasks/{task_id}` returns the agent's final message
  in the `agent_message_final` field; the `agent_message` field only exists on the list endpoint. You
  have to read both — otherwise the report arrives as "(no summary)".
- **Model.** The `model` field is only accepted by the task `POST`; it is absent from the task list.
- **The un-prefixed surface.** A few endpoints live **without** the `/v1` prefix — `POST
  /sandbox/{id}/keepalive` (`/v1/sandboxes/{id}/keepalive` gives a 404; body `{"until": <unix>}`,
  ceiling `SANDBOXD_KEEPALIVE_MAX_SECONDS` = 24 h) and `POST /sandbox/{id}/exec` (body `{"cmd":
  [...]}`, answers `{"stdout","stderr","exit_code"}`, optional `"stream": true`). **`exec` runs a
  command without spawning the agent** — for anything mechanical (start a dev server, inspect the
  workspace) it costs nothing where `submit_task` costs a whole model invocation. It also bumps
  `last_active_at`, unlike the task API.
- **API address.** From inside the `sandboxd_net` network — `LOOP_SANDBOXD_URL=http://sandboxd:9000`.
  The host-side `127.0.0.1:9090` **also serves `/v1`** with the same `Authorization: Bearer` header
  (verified 2026-08-06; an earlier note here claimed it answers 400 — that is no longer true). Handy:
  a probe can be a plain `curl` script over ssh instead of a python file inside the container.
- **409 on create_sandbox** = the response was lost, the sandbox already exists → we adopt the app's
  `current_sandbox_id` (`e243b13`). **409 on submit_task** = the sandbox is not ready yet **or** someone
  else's task is stuck in it → retry until the stage deadline, or wait the stuck task out via
  `list_tasks` (`f77959c`, `c5a3a2f`).
- **`idle_policy` is not passed through** `POST /v1/apps/{id}/sandbox` (the body only takes
  template/ports/runtime_preset/image), so the "make the sandbox always_on" route is closed without
  patching sandboxd itself.
- **Preview** is native: `GET /v1/sandboxes/{id}` → `preview.url`. The domain and TLS are configured by
  sandboxd's own variables (`PREVIEW_DOMAIN`, `PREVIEW_TLS`) — see [[ops/vps]].

## What the agent inside the sandbox actually sees

Verified by a probe on 2026-08-04 (claude 2.1.220, headless `-p --dangerously-skip-permissions`):

- `runtimed` starts the agent with `cmd.Dir = /home/sandbox/workspace/app` — that is, inside the repo
  clone. So the `CLAUDE.md` and `.claude/skills/` **of the target repo itself** reach the agent, as long
  as they are committed (`cmd/runtimed/claude.go`, `task.go`).
- Almost all tools are available, including `Agent`, `Workflow`, `Skill`, `WebSearch` — the probe
  actually executed a `Workflow` with a minimal script. So the prompt line "use the
  parallel-plan-execution skill if it is available" really does fire.
- Child processes spawned by the agent (git, Playwright) inherit the environment variables — the
  `agentenv.go` filter only applies to spawning the agent itself. That is exactly what makes file-based
  secret delivery work.

## Sandbox resource limits are hardcoded, and there is no CPU ceiling

Read from the sources on 2026-08-06 — the same literal block appears twice, in
`control-plane/internal/sandboxspec/spec.go` (`Spec`) and `control-plane/internal/api/handlers.go`
(the direct-create path):

```go
CPUShares:   100,      // relative weight under contention, NOT a ceiling
Memory:      "10g",    // larger than the whole host RAM -> no limit in practice
MemorySwap:  "10g",    // == Memory -> swap is disabled inside the container
PidsLimit:   1024,
```

Three consequences:

- **`docker run --cpus` is not supported at all** — `docker.RunSpec` has no field for it
  (`internal/docker/docker.go`). One sandbox can therefore take every core the host has, which is what
  starves dockerd and its embedded resolver ([[concepts/resilience]] §3).
- **Host swap does not reach a sandbox.** `MemorySwap == Memory` disables container swap regardless of
  what the host has, so swap protects the host and the orchestrator, never the Runs.
- **`runtime_preset` is not a size.** It names a framework (`react-vite`, `nextjs`, `fastapi`, …) and
  only picks the web port and the runtime manifest. There is no API-level way to ask for a bigger or
  smaller sandbox.

**Fixed by a local patch on 2026-08-06** — `deploy/sandboxd-patches/0001-per-sandbox-resource-ceilings.patch`
adds `CPUs` to `RunSpec` and moves all three values into `SANDBOXD_CPUS` / `SANDBOXD_MEMORY` /
`SANDBOXD_MEMORY_SWAP`, defaulting to the original literals. **A sandboxd upgrade drops it silently**,
so re-apply after one; the how and the values we run are in
[`deploy/sandboxd-patches/README.md`](../../../deploy/sandboxd-patches/README.md).

## Probe 2026-08-06: a sandbox survives stop/start with its preview route intact

Method: blank app (no git) → sandbox with `ports: [3000]` → a toy `python3 -m http.server` started
through `exec` → `stop` → `start` → observe. Traefik was hit from the host with an explicit `Host:`
header, so the check is of the route, not of DNS.

| Step | Result |
|---|---|
| `preview.url` before / after | **byte-identical** (`s-<sandbox-id>-3000.preview.<preview-domain>`) |
| route while stopped | no response |
| route right after `start` | **`502 Bad Gateway`** |
| route after respawning the server | `200 OK` |
| timings | `stop` 0.4 s · `start` 0.3 s · respawn→200 2.2 s |

**The 502 is the finding.** Traefik never lost the router: its labels live on the container, the
container is the same one (`Exited (0)` → `Up`), so after `start` the route is whole and only the
upstream is missing. Processes do not survive the stop — whatever served the preview has to be started
again, which is one `exec` call, not an agent task.

Consequence: a Run paused in `awaiting_approval` does not need a live sandbox. Its code is already in
the temporary branch, approve/merge/discard are pure GitHub API, and the preview link stays valid
across any number of sleep/wake cycles ([[concepts/publication]], [[ops/vps]]).

## Probe 2026-08-08: the wake path is automatic, and the manifest is what survives it

The 2026-08-06 probe left one thing open — who starts the server after a wake — and the answer turns
the pause from "possible to sleep" into "sleeps by default"
([[decisions/0015-sleep-the-paused-sandbox]]). Method: app on a real repo → sandbox → toy server on
3000 → `stop` → hit the preview host through traefik from the host itself.

| Step | Result |
|---|---|
| hit the preview host while **stopped** | `200` after **8.3 s** — the container was started *by the request* |
| the same hit, second time | `502` — the sandbox was awake, but nothing had restarted the server |
| after uploading `sandbox.yaml` (`web.command` + `web.port`), `stop`, then hit | `502` at 14.3 s, then **`200`** on the next hit; stable 200 (3 ms) afterwards |
| final state | `status: running`, `preview: ready, last_http_status 200`, `processes: [{web, running: true, restarts: 0}]` |

Two facts behind it, read off the sources rather than guessed:

- **`traefik/dynamic/wake.yml`** declares a file-provider catch-all `sandbox-wake` at **priority 1**
  matching `HostRegexp(^s-<id>-<port>\.preview\..+$)` and forwarding to sandboxd. Per-sandbox routers
  are emitted as container labels at **priority 100**, so the catch-all only ever fires for a preview
  host whose container is stopped. That is the whole wake trigger — no polling, no hook of ours.
- **`cmd/runtimed/process.go`** runs a manifest process with `bash -lc` in the app directory and
  restarts it (with backoff, `maxFastFails`) whenever the container starts. So a server declared in
  `sandbox.yaml` comes back; a server started through `exec` with `nohup` does not.

Manifest contract worth remembering (`internal/manifest`): `version: 1` is required, the top-level key
set is **closed** (`version`, `web`, `workers`, `build` — anything else is an error, not a warning), and
`web.command` without `web.port` is an error. `POST /v1/runtime/manifest/validate` checks a blob
statelessly, which is cheaper than discovering a rejection as a missing preview.

## Links

- [[concepts/secrets-delivery]] · [[concepts/agent-steering]] · [[concepts/resilience]]
- [[components/clients]] — `SandboxdClient`, a thin wrapper over this API
- [[ops/vps]] · [[ops/sandbox-image]]
