# Overview — where the project stands

> "Current state of the world". Updated whenever the focus shifts. **Does not copy** plan checkboxes
> (they live in `docs/superpowers/plans/`) and does not retell the specs — it only links. The date of
> the last update is in `log.md`.

## Phase

**A working system in production, five phases behind us. The last week was not new phases but
closing the holes that running against production repositories exposed.**

The whole loop: an issue with `loop:ready` → the planner writes a spec and a plan ⇄ Implementor
Advisor → PR with `loop:run` → executor in a fresh sandbox → review → e2e with Playwright and video →
contract capture for the tasks this one blocks → publication to a temporary branch → pause with a
preview link and Telegram buttons → approve →
fast-forward of the PR branch → merge (optionally with promotion to staging). All of it lives in a
single FastAPI service on SQLite with an in-process asyncio worker.

| Phase | What it delivered | Status |
|---|---|---|
| 1 — loop core (MVP) | webhook → sandbox → execute → publication → Telegram | deployed, accepted by smoke test |
| 2 — Reviewer | review before publication, fix loop, escalation with a label | deployed, accepted by smoke test |
| 3 — E2E | Playwright scenarios, verdict, video in Telegram | deployed, accepted by smoke test |
| Telegram v2 | forum topic per Run, live progress card, feature name | deployed, accepted by smoke test |
| 4a — control plane | pause with preview, approve/merge/cancel/restart buttons, revise replies | deployed, smoke test fully passed |
| 5a — backlog | scheduler on top of GitHub Issues, planning Run, lanes, native `blocked_by` | deployed, accepted by smoke test (cross-repo included) |

## Current focus

Running against the production repositories `<backend-repo>` and `<frontend-repo>`
([[ops/target-repos]]) and closing whatever that exposes. Closed on 2026-08-04…05: secrets delivered
as a file ([[decisions/0002-secrets-as-file]]), the hidden ~35 min ceiling per Run
([[decisions/0003-keepalive-against-idle-reaper]]), invisible image skills
([[decisions/0004-skills-into-sandbox-skel]]), crashes on transient API failures
([[decisions/0005-transient-resume-budget]]), silent merge of a red PR
([[decisions/0006-merge-gate-and-conflict-resolver]]), runs dying from host overload
([[decisions/0009-concurrency-cap-and-poll-resilience]]).

Closed on 2026-08-06: every stage now opens its own Claude session
([[decisions/0013-one-session-per-stage]]) after profiling put 61% of a Run's bill on cache
writes, and that bill is now observable rather than guessed at — [[components/tracing]].

Closed on 2026-08-10: a dependency between issues carries a payload instead of just "wait". A Run
whose issue blocks another one ends with a `contracting` stage that describes the interface it built;
the digest reaches every dependent task's planner through `.loop/task.md` and the authoritative
sources through `.loop/context/`, and both the planner and the Advisor now refuse an endpoint they
cannot trace to a source ([[concepts/contract-handoff]]).

## Known and still open

- **Orphaned sandboxes after a Run crashes.** The fail path does not always reach `delete_app` — the
  sandbox keeps burning CPU and the subscription quota. Cleaned up by hand ([[ops/vps]]).
- **Host-level fixes do not survive a host move.** The image-prune cron came back with `-af` on the
  new box and deleted the sandbox images again on 2026-08-08 ([[ops/vps]]); the images and the anchor
  containers were restored the same day. Nothing now watches for their absence — the first Run after
  a prune is still the detector, it just fails in seconds instead of hours ([[concepts/resilience]] §6).
- **VPS resources — the host was replaced on 2026-08-06.** One Run costs a whole core and ~3.5 GB
  (measured, [[ops/vps]]), so the old 2-core box never had headroom for its cap of 2. The stack now
  runs on 4 vCPU / 16 GB / 4 GB swap with the cap at 3
  ([[decisions/0012-one-bigger-host-over-a-multi-host-pool]]); the Claude OAuth, the API keys and the
  git credential migrated with the sandboxd state, so no re-authentication was needed. A sandbox now
  also has real ceilings — 3 cores, 5 GB, 2 GB of swap — via a local patch to sandboxd
  ([`deploy/sandboxd-patches/`](../../deploy/sandboxd-patches/README.md); **a platform upgrade drops
  it silently**). Closed on 2026-08-08: a paused Run no longer holds a live sandbox — it sleeps and its
  preview link wakes it ([[decisions/0015-sleep-the-paused-sandbox]]), so the cap now describes the
  host rather than the worker. What remains is arithmetic: five concurrent Runs need 6 vCPU / 19.5 GB
  by the measured rule, so the boxed capacity is three.
- **The contract handoff has not run live yet.** Unit tests cover every branch, but the two-repository
  smoke check — the producer's marked comment, the consumer's `.loop/context/`, and above all the
  human-correction path where an edited comment outranks the stored row — is still pending
  ([[concepts/contract-handoff]]).
- **Exclusive backlog tasks can starve** — accepted option A from the phase 5 spec.
- **`e2e.services` in `.loop.yml`** is still "not supported yet"; the stack is brought up by a script
  from `run:` — a working path, verified by the two-repo smoke test.
- **Promotion via `promote:staging` is wired only in the backend**; in the frontend the Merge &
  Deploy button will not trigger a deploy.
- **`closeForumTopic` is not supported in a private threaded chat** — the topic stays open and is
  only renamed. Behaviour accepted.
- **Open-sourcing is prepared but not done.** The documents are English
  ([[decisions/0010-documentation-in-english]]) and carry no environment specifics
  ([[decisions/0011-no-environment-specifics-in-the-repo]]), but **git history still holds the
  pre-sanitisation versions** of those files, and no licence, README or contribution guide exists yet.

## Where to look next

Phase specs and plans — [`docs/superpowers/`](../superpowers/). Architecture summary and commands —
[`CLAUDE.md`](../../CLAUDE.md). Everything known about the behaviour of the platform underneath us —
[[concepts/sandboxd-platform]].
