# 0003 — Keepalive against the sandboxd idle reaper

- **Status:** accepted
- **Date:** 2026-08-04
- **Related:** [[concepts/resilience]] · [[concepts/sandboxd-platform]] · commit `e424cca`

## Context

A hidden ceiling of ~35 minutes on a whole Run showed up. The sandboxd idle reaper
(`internal/reaper/idle.go`) does a `docker stop` on any running sandbox whose `last_active_at` is
older than `SANDBOXD_IDLE_THRESHOLD_SECONDS=2100`. The field is bumped **only** by sandbox creation
and the exec endpoints; the asynchronous task API (`v1_tasks.go`, `taskwatch.go`) neither bumps it nor
registers an in-flight exec. So an agent working longer than the threshold was killed mid-task
regardless of `timeout_minutes` — Run#29 (~30 min) finished five minutes short of being cut off.

The "make the sandbox `always_on`" route is closed: `POST /v1/apps/{id}/sandbox` does not forward
`idle_policy`.

## Decision

The orchestrator extends the sandbox's life itself through the **internal** endpoint
`POST /sandbox/{id}/keepalive` (no `/v1` prefix; `/v1/sandboxes/{id}/keepalive` returns 404), body
`{"until": <unix>}`. In code: `SandboxdClient.keepalive()` (best-effort, swallows errors),
`Pipeline._poll_wait()` instead of a bare `asyncio.sleep` in every polling loop, a separate keepalive
covering the whole preview TTL on entering `awaiting_approval`, and `_sleep_awake()` for long pauses.
The window is `LOOP_KEEPALIVE_MINUTES` (30).

## Alternatives

- *Raise the reaper threshold globally* — rejected: it is an instance-wide setting, it hits every
  sandboxd consumer on the host, and abandoned sandboxes would live forever.
- *Patch sandboxd so the task API bumps `last_active_at`* — rejected: someone else's service.
- *Keep an exec ping instead of keepalive* — rejected: extra load for the same effect that comes cheaper.

## Consequences

- The keepalive window is deliberately short: it bounds how long an **abandoned** sandbox survives.
- Any new pause in the code must extend the keepalive — otherwise the same breakage comes back
  (which is exactly what happened with Run#40's hour-long rate-limit pause, fixed by `_sleep_awake`).
- The reaper stops rather than deletes, so `start_sandbox` + `_ensure_awake` bring the sandbox back to
  life without losing the workspace or the agent session.
