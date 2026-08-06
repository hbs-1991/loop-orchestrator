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
| **The sandbox home directory is not what is in the image** | sandboxd bind-mounts a per-sandbox loopback workspace over `/home/sandbox`; the home is seeded once from `/opt/sandbox-skel` (`control-plane/internal/loopback/loopback.go`) | skills are placed into `/opt/sandbox-skel/.claude/skills/` — [[decisions/0004-skills-into-sandbox-skel]] |
| **The Files API dies together with the app** | after `delete_app` the whole workspace returns 404 "no such directory" | file diagnostics and video export are only possible while the Run is alive; `_send_e2e_videos` must run **before** `delete_app` |

## Small but biting API details

- **Task summary.** A single `GET /v1/sandboxes/{id}/tasks/{task_id}` returns the agent's final message
  in the `agent_message_final` field; the `agent_message` field only exists on the list endpoint. You
  have to read both — otherwise the report arrives as "(no summary)".
- **Model.** The `model` field is only accepted by the task `POST`; it is absent from the task list.
- **Keepalive is an internal surface.** `POST /sandbox/{id}/keepalive` **without** the `/v1` prefix
  (`/v1/sandboxes/{id}/keepalive` gives a 404). It works from inside the docker network, body is
  `{"until": <unix>}`, ceiling is `SANDBOXD_KEEPALIVE_MAX_SECONDS` (24 h).
- **API address.** The host-side `127.0.0.1:9090` returns 400 on `/v1/...`; you must go from inside the
  `sandboxd_net` network — `LOOP_SANDBOXD_URL=http://sandboxd:9000`.
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

## Links

- [[concepts/secrets-delivery]] · [[concepts/agent-steering]] · [[concepts/resilience]]
- [[components/clients]] — `SandboxdClient`, a thin wrapper over this API
- [[ops/vps]] · [[ops/sandbox-image]]
