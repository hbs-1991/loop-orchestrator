# Decisions — choices made during implementation

ADR-lite: decisions that are **not in the specs** (`docs/superpowers/specs/`). If a decision is a
product decision and the spec describes it, it lives there and the wiki only links to it.

New decision: a file `NNNN-<slug>.md` following [`_template.md`](_template.md), a row in this
table and in [../index.md](../index.md), an entry in [../log.md](../log.md).

| # | Decision | Date |
|---|---|---|
| [0001](0001-llm-wiki-memory-system.md) | The LLM wiki as project memory: boundaries, structure, hooks | 2026-08-06 |
| [0002](0002-secrets-as-file.md) | Secrets travel as the file `.loop/secrets.env` — app config never reaches the agent | 2026-08-05 |
| [0003](0003-keepalive-against-idle-reaper.md) | Keepalive against the idle reaper: a hidden ~35 min ceiling per Run | 2026-08-04 |
| [0004](0004-skills-into-sandbox-skel.md) | Image skills go into `/opt/sandbox-skel`; the sandbox home is shadowed by a mount | 2026-08-04 |
| [0005](0005-transient-resume-budget.md) | A transient API failure resumes the session; budget of 10 | 2026-08-05 |
| [0006](0006-merge-gate-and-conflict-resolver.md) | Merge is gated on CI; conflicts are resolved by a background agent with a temporary token | 2026-08-05 |
| [0007](0007-promote-label-and-base-branch.md) | Promotion via the `promote:staging` label; `base_branch` is read from the default branch | 2026-08-05 |
| [0008](0008-english-everywhere.md) | English in every system-facing text; Russian only in project documents (document carve-out withdrawn by 0010) | 2026-07-31 |
| [0009](0009-concurrency-cap-and-poll-resilience.md) | A cap of 2 concurrent Runs; polling tolerates transport failures | 2026-08-05 |
| [0010](0010-documentation-in-english.md) | Every document in the repository is English — the project is headed for open source | 2026-08-06 |
| [0011](0011-no-environment-specifics-in-the-repo.md) | No environment specifics in documents: hosts, domains, accounts and repo names are placeholders | 2026-08-06 |
| [0012](0012-one-bigger-host-over-a-multi-host-pool.md) | One bigger host (4 vCPU / 16 GB) instead of a pool of small ones; measured sizing rule | 2026-08-06 |
| [0013](0013-one-session-per-stage.md) | One Claude session per stage, and the `continue` tri-state | 2026-08-06 | accepted |
| [0014](0014-hand-rolled-otlp-emitter.md) | A hand-rolled OTLP emitter, not the OpenTelemetry SDK | 2026-08-06 | accepted |
| [0015](0015-sleep-the-paused-sandbox.md) | The paused sandbox sleeps; its preview wakes it on demand via `sandbox.yaml` | 2026-08-08 | accepted |
