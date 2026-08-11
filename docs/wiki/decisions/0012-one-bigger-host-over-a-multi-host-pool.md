# 0012 — One bigger host, not a pool of small ones

- **Status:** accepted
- **Date:** 2026-08-06
- **Related:** [[ops/vps]] · [[concepts/resilience]] · [[concepts/sandboxd-platform]] ·
  [[decisions/0009-concurrency-cap-and-poll-resilience]]

## Context

Two parallel Runs saturate the 2-core host: measured on 2026-08-06, each sandbox holds a full core and
~3.0–3.5 GB while everything else on the box — orchestrator, sandboxd, traefik, caddy, console —
together costs 0.22% of a core and 270 MB. RAM peaked at 8.2 GB of 8 with no swap. The cap of 2 from
[[decisions/0009-concurrency-cap-and-poll-resilience]] does not create headroom, it only rations a host
that has none.

Nothing in the code can fix this. The load is the work itself — installs, builds, test suites,
Playwright — and sandboxd offers no CPU ceiling to shape it with ([[concepts/sandboxd-platform]]). So
the question was only which hardware shape to buy.

Two candidate shapes were on the table, plus a commercial pay-per-second sandbox provider. This is not
in any spec: the specs describe the loop, not the machine it runs on.

## Decision

**Move the orchestrator to a 4 vCPU / 16 GB host and raise the cap to 3 Runs.** The current 2/8 box
keeps the unrelated service that already shares it, and becomes a candidate second host later if three
Runs ever stop being enough.

Sizing follows the measured rule in [[ops/vps]]: N Runs need `(N+1)` vCPU and `(3.5·N + 2)` GB.

## Alternatives

- **A second small VPS with Runs distributed across both.** Rejected for now. Capacity fragments — 2/8
  plus 4/16 fits 1 + 3 Runs where a single 8/32 fits six — and the price is real: the sandboxd API
  would have to be exposed off-host (today it is only reachable inside `sandboxd_net`, so this would
  be the system's first remote attack surface), every Run would need host affinity in the DB with a
  client pool and a load-aware pick, `recover()` would have to know the host, and the 4.5 GB sandbox
  image, the preview wildcard DNS record and the orphan sweep would all double. Worth doing when one
  host genuinely cannot hold the load — not before.
- **One 8 vCPU / 32 GB host instead of 4/16.** Not rejected, deferred: 4/16 triples current capacity
  for less money, and the measured rule makes the next step a calculation rather than a guess.
- **Commercial per-second sandboxes (Daytona and friends).** Priced out at roughly $0.33–0.67 per
  2-hour Run for a 2 vCPU / 4 GiB box, i.e. $30–150/month at 2–10 Runs a day — competitive only at low
  volume, and it costs the whole agent-runtime layer: sandboxd's `runtimed` runs Claude Code, holds
  the session, resumes it after a stream drop and reports the final message, and a bare container
  provider gives none of that. The upside is real (git push from the sandbox, mutable branches, env
  vars that reach the agent — three of this project's standing workarounds would disappear), so this
  is worth a pilot on free credit, not a migration.

## Consequences

- `LOOP_MAX_CONCURRENT_RUNS` becomes 3 after the move; it stays an ops setting, never a code constant.
- The measured sizing rule in [[ops/vps]] is now the authority for the next capacity question. Re-measure
  it if the target repositories change shape — it is a property of their builds, not of the loop.
- Sandbox CPU is still uncapped. A bigger host reduces the blast radius but does not remove it; a
  `--cpus` ceiling (a patch to sandboxd's `RunSpec`, or an external `docker update`) and host swap
  remain open work.
- **Sleeping the paused sandbox is now unblocked and stays worth doing.** The probe of 2026-08-06
  proved a sandbox survives stop/start with its preview route intact
  ([[concepts/sandboxd-platform]]), and a paused Run needs no sandbox for approve or merge — so the
  pause can cost nothing instead of holding a dev server for two hours outside the concurrency cap.
  It frees memory rather than cores, which is why it is not a substitute for this decision.
