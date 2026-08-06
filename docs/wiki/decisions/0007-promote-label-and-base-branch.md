# 0007 — Promotion to staging via a label; the PR base comes from `.loop.yml` on the default branch

- **Status:** accepted
- **Date:** 2026-08-05 (base branch — 2026-08-04)
- **Related:** [[concepts/publication]] · [[ops/target-repos]] · commits `04cc60e`, `1c1c9ab`

## Context

<org>'s trunk is `main`, while the auto-deploy to staging runs from the `staging` branch, and
the user wanted a merged loop run to trigger a rollout.

The first attempt — `base_branch: staging` in the backend's `.loop.yml` — was withdrawn by that repo's
own agent: it enforces the invariant "`staging` is always a fast-forward of `main`, never a fork"
(ADR 0049 of that repository, `--ff-only`). So the deploy has to be a **separate explicit step**, not a
side effect of the PR base.

## Decision

1. **`base_branch` in `.loop.yml`** stays as a mechanism (two places used to hardcode the default
   branch: `scheduler.bootstrap` and `pipeline._publish_plan`), but it is read **from the repository's
   default branch** via `loopconfig.resolve_base_branch` — an override cannot dictate where to look for
   itself. No config, or a broken one → silently the default branch; a real parse error surfaces at
   `preparing`. The branch-off point and the base must match.
2. **The `🚀 Merge & Deploy` button** (callback `md:`, action `merge_deploy`): it puts
   `LOOP_PROMOTE_LABEL` (default `promote:staging`) on the PR **before** the merge — the target repo's
   promote workflow reads the labels of the already-merged PR. If the label fails to attach, the merge
   is not performed; if the merge is refused, the label is removed.

## Alternatives

- *Merge straight into `staging` (`base_branch: staging`)* — rejected by the repository owner: it breaks
  the ff-from-main invariant.
- *Deploy on every merge* — rejected: promotion is opt-in per change, ordinary merges roll out nothing.

## Consequences

- An orphaned label would turn a later ordinary Merge into a silent deploy — hence the label rollback on refusal.
- The `.loop.yml` format is a Locked Decision, so the MVP spec was updated in the same commit.
- In the frontend nobody reads `promote:staging` yet: the button is harmless but will not trigger a deploy.
- The GitHub mechanics everything rests on: `workflow_dispatch`/`repository_dispatch` are documented
  exceptions from `GITHUB_TOKEN` recursion suppression (confirmed live), no PAT needed.
