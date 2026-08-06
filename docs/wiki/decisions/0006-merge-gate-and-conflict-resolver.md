# 0006 — Merge is gated on CI, and conflicts are resolved by a background agent

- **Status:** accepted
- **Date:** 2026-08-05
- **Related:** [[concepts/publication]] · [[ops/target-repos]] · commits `5069e54`, `91a8d5e`, `35d1330`

## Context

The Merge button merged the PR unconditionally. Production repositories have **no branch protection**,
so a red PR#13 got merged and carried a broken `uv.lock` into main. The source of the breakage turned
out to be the **planner** itself: it committed a regenerated lock without the dev group, and ruff came
"unpinned".

Separately: a PR behind a protected base refused to merge, and a conflicting one required a manual
worktree — that is how PR#13 was handled, which conflicted on the append hotspot `docs/wiki/log.md`
(the loop agent and the user's agent were writing the journal in parallel).

## Decision

1. `Actions._merge_readiness` reads the head sha's check runs: `failure`/`timed_out`/`cancelled`/
   `action_required` → refusal naming the checks; running ones → "press it once they finish"; a
   repository with no checks merges as before. **An empty list means "not yet", not "there is no CI"**
   (`35d1330`).
2. `behind` with a protected base → `update-branch` and a request to press again.
3. `mergeable: false` → a background resolver agent: a fresh app on the PR branch, the temporary secret
   `GIT_SYNC_TOKEN` as a file, merge+resolve, push to a new `loop/run-N-sync`, ff of the PR branch,
   automatic merge retry preserving the promotion label.
4. The planner prompt is **forbidden to touch lockfiles** — commit only the spec and the plan,
   `git checkout --` everything else.

## Alternatives

- *Rely on branch protection in the target repos* — rejected as the only measure: the rules are set by
  the owner and may simply not exist; the gate in the orchestrator always works. (A rulesets guide was
  handed to the user anyway — that is the second line.)
- *Resolve conflicts by hand* — rejected: a manual worktree per conflict eats the point of the automation.
- *Give the sandbox permanent git credentials* — rejected: the token lives only inside the resolver
  agent and only for the duration of the merge.

## Consequences

- A merge may be refused — that is a normal outcome, not an error; the refusal text names the specific checks.
- The resolver agent is the only place where a write-capable token enters a sandbox.
- An orphaned promotion label is dangerous: on a refused merge it is removed
  ([[decisions/0007-promote-label-and-base-branch]]).
