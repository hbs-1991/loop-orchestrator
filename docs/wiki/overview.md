# Overview — where the project stands

> "Current state of the world". Updated whenever the focus shifts. **Does not copy** plan checkboxes
> (they live in `docs/superpowers/plans/`) and does not retell the specs — it only links. The date of
> the last update is in `log.md`.

## Phase

**A working system in production, five phases behind us. The last week was not new phases but
closing the holes that running against production repositories exposed.**

The whole loop: an issue with `loop:ready` → the planner writes a spec and a plan ⇄ Implementor
Advisor → PR with `loop:run` → executor in a fresh sandbox → review → e2e with Playwright and video →
publication to a temporary branch → pause with a preview link and Telegram buttons → approve →
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

## Known and still open

- **Orphaned sandboxes after a Run crashes.** The fail path does not always reach `delete_app` — the
  sandbox keeps burning CPU and the subscription quota. Cleaned up by hand ([[ops/vps]]).
- **VPS resources.** 2 cores / 8 GB / no swap; the cap of 2 parallel Runs is a host limitation, not
  an architectural one.
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
