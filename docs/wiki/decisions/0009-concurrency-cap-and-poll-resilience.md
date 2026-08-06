# 0009 — A cap of 2 concurrent Runs and failure-tolerant polling

- **Status:** accepted
- **Date:** 2026-08-05
- **Related:** [[concepts/resilience]] · [[ops/vps]] · commit `25b02d6`

## Context

Runs #41 and #42 died on `ConnectError('[Errno -3] Temporary failure in name resolution')` while their
planners kept working. Three concurrent runs drove the 15-minute load average to **19 on two cores**;
dockerd stopped answering the embedded resolver 127.0.0.11 in time, glibc returned EAI_AGAIN, and
`with_retries` (3 attempts ≈ 6 s) does not sit out a failure that long.

The sandboxes of the dead runs stayed alive and burned CPU and the subscription limit for another half
hour — the fail path never reached `delete_app`. After a manual cleanup the load dropped from 19 to 0.5.

## Decision

1. `Pipeline._task_status` returns `None` on `httpx.TransportError` and 5xx — the polling loops simply
   wait for the next tick, and the stage deadline stays the boundary. **A 4xx still fails the Run**: a
   task that does not exist will not come back.
2. `_sleep_awake` refreshes the keepalive twice during a long pause (an hour-long rate-limit pause is
   longer than both the keepalive window and the reaper threshold — that is how Run#40's sandbox died).
3. `SandboxdClient.start_sandbox` + `_ensure_awake`: the reaper **stops** the container rather than
   deleting it, so `POST /v1/sandboxes/{id}/start` brings the sandbox back to life.
4. An ops constraint on the VPS: `LOOP_MAX_CONCURRENT_RUNS=2` — four sandboxes at ~2 GB each do not fit
   into 8 GB without swap.

## Alternatives

- *Raise `with_retries` to dozens of attempts* — rejected: it treats the symptom and leaves the host
  overloaded; and it does not save the sandboxes of the dead runs.
- *Get a bigger VPS* — not rejected but deferred: first we have to stop knocking the host over with three runs.

## Consequences

- A lost polling tick no longer kills a Run whose agent is still working.
- Orphaned sandboxes after a failure are a known open hole: the fail path does not always reach
  `delete_app`, check by hand ([[ops/vps]]).
- The cap of 2 runs is an operational setting, not a code constant; revisit it when moving to a larger host.
