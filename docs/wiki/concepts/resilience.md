# Concept: resilience — what breaks in production and how it is fixed

A Run lives for hours and depends on four unreliable things: the model API, sandboxd, GitHub and the VPS
itself. Every item below was written off the back of a failed run.

## 1. The agent's stream drops

**Symptom:** `API Error: Response stalled mid-stream`, the task ends in an error, the Run fails.

**Fix:** both polling loops (`_execute`, `_run_sandbox_task`) resubmit the task with
`continue_session=true` — the agent session survives the drop, so the resume continues the work rather
than starting over. The markers are `TRANSIENT_AGENT_MARKERS` (api error, connection error, econnreset,
socket hang up, fetch failed). The budget is `LOOP_AGENT_RETRY_ATTEMPTS` (10) per stage, with a
`LOOP_AGENT_RETRY_BACKOFF_SECONDS` (120) pause before each resume: drops come in clusters (observed
roughly every 5 min), and an immediate resume jumps straight back into the same hole. The rate-limit
branch is checked first.

The budget was raised twice: 2 → 4 → 10. During the Anthropic incident of 2026-08-05 (Fable/Opus/Mythos
degradation) Run#34/#35 burned their resumes against one and the same incident and died, throwing away
real progress. 10 matches Claude Code's own retry default
([[decisions/0005-transient-resume-budget]]).

## 2. The sandboxd idle reaper (the ~35-minute ceiling)

The reaper stops any sandbox whose `last_active_at` is older than 2100 s, and the async task API does
**not** bump that field. The fix is keepalive: `Pipeline._poll_wait` instead of a bare `asyncio.sleep` in
**every** polling loop, a separate keepalive covering the whole preview TTL on entering
`awaiting_approval`, and `_sleep_awake` for long pauses
([[decisions/0003-keepalive-against-idle-reaper]]).

The hour-long rate-limit pause is a special case: it is longer than both the keepalive window (30 min)
and the reaper threshold, and nobody polls during the pause. So `_sleep_awake` refreshes the keepalive
twice per window. The reaper did manage to kill Run#40's sandbox in exactly such a pause.

If the sandbox does get stopped, `_ensure_awake` brings it back up: the reaper **stops** the container,
it does not delete it, so the volume with the workspace and the session is intact.

## 3. VPS overload breaks docker's DNS

**Incident 2026-08-05.** Runs #41/#42 died on
`ConnectError('[Errno -3] Temporary failure in name resolution')`. The cause: three parallel runs on
**2 cores / 8 GB with no swap** drove the load average to 19, dockerd stopped keeping up with the
embedded resolver at 127.0.0.11, glibc returned EAI_AGAIN, and `with_retries` (3 attempts ≈ 6 s) cannot
sit out a failure like that.

Worse, the sandboxes of the failed runs stayed **alive** and burned CPU and subscription limit for
another half hour — the failure path never reached `delete_app`. After a manual cleanup the load dropped
from 19 to 0.5.

**Fix:** `Pipeline._task_status` returns `None` on `httpx.TransportError` and 5xx — the polling loop
simply waits for the next tick, and the stage deadline remains the boundary; a 4xx still fails the Run
(a task that does not exist will not come back). Plus the ops constraint `LOOP_MAX_CONCURRENT_RUNS=2`
([[decisions/0009-concurrency-cap-and-poll-resilience]]).

**What the numbers turned out to be (measured 2026-08-06).** A sandbox mid-stage takes a whole core
and ~3.5 GB, while the entire control plane — orchestrator, sandboxd, traefik, caddy — takes 0.22% of
one core together. Two Runs on two cores therefore leave dockerd exactly nothing, and the cap of 2 was
never a safe setting on this host, only a less unsafe one. Full profile and the sizing rule are in
[[ops/vps]].

**The cap was long the only lever we had.** sandboxd set no CPU ceiling on a sandbox at all — its
container spec hardcoded `CPUShares: 100` (a relative weight, not a limit) and no `--cpus`, and
`MemorySwap == Memory` meant host swap never reached a sandbox ([[concepts/sandboxd-platform]]). Since
2026-08-06 a local patch gives us both (`SANDBOXD_CPUS`, `SANDBOXD_MEMORY`, `SANDBOXD_MEMORY_SWAP` —
[`deploy/sandboxd-patches/`](../../../deploy/sandboxd-patches/README.md)), and the host is now sized
for its Runs ([[decisions/0012-one-bigger-host-over-a-multi-host-pool]]). The two answer different
halves of the same failure: the ceiling stops one Run taking the last core, the sizing stops three
Runs fighting over four.

**A Run takes every core it is given.** The "one core per Run" figure was measured with two Runs
sharing two cores. On four cores a single planner was seen at **319%** — so the sizing rule describes
a steady state under contention, not an appetite. Without a ceiling, one Run is enough to starve the
host; that is why the patch matters even on a bigger box.

## 4. Transient HTTP client errors

`clients/retry.with_retries` — 3 attempts with backoff on 5xx and transport errors; the GitHub and
sandboxd clients stand on it. That is enough for a blip, but **not enough** for a host-level failure —
hence item 3.

From the same series: twice on 2026-08-05 the GitHub Actions runners could not reach the VPS on port 22,
so `deploy.yml` now tries the ssh connection with retries (8×20 s) before delivering (`8780aa8`).

## 5. Orchestrator restart in the middle of a stage

`Worker.recover()` + `_submit_resumable`/`_drain_stale_task`: if a task is running in the sandbox, it is
waited out rather than having a second one submitted into a busy sandbox (409 → this used to mean
`failed`, that is how Run#15 died).

## 6. A dead sandbox answers 409 forever

**Incident 2026-08-08.** A planning Run spent 53 minutes in `planning` without a single `run_events`
row, a Telegram push or a submitted task — and would have spent three hours, its whole
`timeout_minutes`. Its sandbox had died at birth: the nightly prune had deleted the sandbox image, so
the workspace seed failed and sandboxd left the sandbox in status `error` ([[ops/vps]]).

Two of our own mechanisms turned that into silence:

- `create_sandbox` adopts `current_sandbox_id` on a `409`, which is right when a lost reply made
  `with_retries` re-POST an already-created sandbox — but the retry of a **failed** creation lands on
  the same branch, and it adopted a stillborn sandbox. It now reads the status first and refuses
  anything in `error`.
- `_submit_resumable` reads every `409` as "busy, wait" and retries until the stage deadline. A
  sandbox in `error` answers `409` too and never stops; the loop now asks sandboxd for the status on a
  conflict and fails the stage at once (`_sandbox_is_dead`). Deliberately one-sided: an unreachable
  control plane or a version that reports no status is **not** read as death, or a Run that is merely
  waiting would be killed.

The general lesson is the one this page keeps re-learning: **a retry loop without a liveness check is
a silent timeout**. Every state that can never resolve must be recognised, not waited out.

## The post-mortem rule

A failed Run is not "a flake" until the cause is found: three of the four items above looked like chance
and turned out to be systemic. The post-mortem is written into `log.md` with type `incident`.

## Links

- [[components/pipeline]] · [[components/worker-and-scheduler]] · [[concepts/sandboxd-platform]]
- [[ops/vps]] — host resources and cron gotchas
