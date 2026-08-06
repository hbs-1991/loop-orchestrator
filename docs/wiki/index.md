# Wiki Index — page catalogue

> Map of the project's LLM wiki. The agent reads it first (mixed in by the `SessionStart` hook), then
> drills down into whichever pages it needs. New page → a row here. What the wiki is and how to keep
> it — [[conventions]].

## Root pages

| Page | Purpose |
|---|---|
| [overview](overview.md) | "Where we are now": phases, what is deployed, what is unfinished |
| [log](log.md) | Chronological journal (append-only, newest on top) |
| [conventions](conventions.md) | Wiki schema and upkeep rules (ingest / query / lint) |

## Concepts — cross-cutting mechanics

| Page | About |
|---|---|
| [sandboxd-platform](concepts/sandboxd-platform.md) | Real platform behaviour: what a probe or the sources proved, not what the docs claim |
| [run-lifecycle](concepts/run-lifecycle.md) | Two kinds of Run, states, invariants, recovery after a restart |
| [publication](concepts/publication.md) | Two-phase publication, merge gates, promotion to staging |
| [secrets-delivery](concepts/secrets-delivery.md) | Secrets as the `.loop/secrets.env` file, and why not via app config |
| [agent-steering](concepts/agent-steering.md) | Five channels for steering the agent: prompt, model, image skills, repo skills, JSON verdict |
| [resilience](concepts/resilience.md) | What breaks in production: stream drops, the idle reaper, VPS overload, restarts |

## Components — code subsystems

| Page | Files |
|---|---|
| [pipeline](components/pipeline.md) | `pipeline.py`, `review.py`, `e2e.py`, `planning.py` — Run stages |
| [worker-and-scheduler](components/worker-and-scheduler.md) | `worker.py`, `scheduler.py`, `issue_tasks.py` — queue and backlog |
| [ingress-and-control](components/ingress-and-control.md) | `webhook.py`, `telegram_webhook.py`, `actions.py`, `main.py` |
| [clients](components/clients.md) | `clients/` — GitHub, sandboxd, Telegram, retry |
| [storage-and-config](components/storage-and-config.md) | `db.py`, `models.py`, `state_machine.py`, `config.py`, `loopconfig.py`, `secrets.py` |

## Ops — live infrastructure

| Page | About |
|---|---|
| [vps](ops/vps.md) | Host, resources, network, domains, the cron image-prune incident |
| [deploy-and-ci](ops/deploy-and-ci.md) | Repository, `ci.yml`/`deploy.yml`, secrets, deliberate deploy limitations |
| [sandbox-image](ops/sandbox-image.md) | Building `loop-sandbox`, skills, image gotchas |
| [target-repos](ops/target-repos.md) | Smoke and production repositories, their `.loop.yml`, provisioning |

## Decisions — decisions made during implementation

Full table — [decisions/README.md](decisions/README.md).

| # | Decision |
|---|---|
| [0001](decisions/0001-llm-wiki-memory-system.md) | LLM wiki as the project's memory |
| [0002](decisions/0002-secrets-as-file.md) | Secrets as a file, not via app config |
| [0003](decisions/0003-keepalive-against-idle-reaper.md) | Keepalive against the idle reaper |
| [0004](decisions/0004-skills-into-sandbox-skel.md) | Skills in `/opt/sandbox-skel` |
| [0005](decisions/0005-transient-resume-budget.md) | Session resume on a transient API failure |
| [0006](decisions/0006-merge-gate-and-conflict-resolver.md) | CI merge gate + conflict-resolver agent |
| [0007](decisions/0007-promote-label-and-base-branch.md) | `promote:staging` and `base_branch` |
| [0008](decisions/0008-english-everywhere.md) | English in every text the system emits |
| [0009](decisions/0009-concurrency-cap-and-poll-resilience.md) | Cap of 2 Runs and tolerant polling |
| [0010](decisions/0010-documentation-in-english.md) | Every document in the repository is English (open source) |
| [0011](decisions/0011-no-environment-specifics-in-the-repo.md) | Environment specifics are placeholders, never literals |

## Sources of truth (don't duplicate — link)

- Phase specs — [`docs/superpowers/specs/`](../superpowers/specs/)
- Phase plans with checkboxes — [`docs/superpowers/plans/`](../superpowers/plans/)
- Repository rules and commands — [`CLAUDE.md`](../../CLAUDE.md)
- Platform installation — [`docs/deploy.md`](../deploy.md)
