# Concept: publishing the code — why it is two-phase

## The constraint

A sandbox cannot `git push` on its own. Push is a host-side sandboxd control plane operation
(`POST /v1/apps/{id}/git/push`), and it can only target **a new branch**: the import branch and
main/master are rejected, force is unavailable, and fetch/pull does not exist.

## The scheme

1. **`staging`** — the agent commits inside the sandbox (`POST /v1/apps/{id}/git/commit`), and the
   orchestrator pushes into a **temporary** branch `loop/run-<id>`. Before the pause, so that the work
   is safe even before a human decides.
2. **the `awaiting_approval` pause** — the summary, the e2e videos and the preview link go to Telegram.
3. **`publishing`** — the orchestrator fast-forwards the PR branch through the GitHub API
   (`PATCH /git/refs`, `force: false`).

On a non-fast-forward the temporary branch is **kept** — the work is not lost, it is sorted out by hand.

The consequence the whole design exists for: **the sandbox dying during the pause is harmless**. The
code is already in `loop/run-<id>`; approve and merge work after the reaper has killed the sandbox
(verified by a TTL-expiry smoke test, 2026-08-03).

## Partial publication

An agent crash on execute is no reason to lose work: `_publish_partial` best-effort publishes the
commits already made, following the same scheme.

## Merge and its gates

The Merge button (and Merge & Deploy) first reads readiness — `Actions._merge_readiness`:

- **red CI** (`failure`/`timed_out`/`cancelled`/`action_required` among the head sha's check runs) →
  refusal, listing the names of the failed checks. The reason for this rule: a red PR#13 got merged and
  carried a broken `uv.lock` into main — production repos have **no** branch protection
  ([[decisions/0006-merge-gate-and-conflict-resolver]]);
- **checks still running** → "press it once they finish"; an **empty list** of checks means "not set up
  yet", not "this repo has no CI" (`35d1330`);
- **branch behind base** (protected base) → `update-branch` and a request to press again;
- **conflict** (`mergeable: false`) → a background resolver agent: a fresh app on the PR branch, a
  temporary `GIT_SYNC_TOKEN` delivered as a file, merge+resolve, push into a new `loop/run-N-sync`,
  ff of the PR branch, and an automatic merge retry preserving the promotion label.

The clone inside the sandbox has **no** credentials, so `git fetch` does not work there — hence the
temporary token for the resolver agent (verified, 2026-08-05).

## Promotion to staging

The `🚀 Merge & Deploy` button puts `LOOP_PROMOTE_LABEL` (default `promote:staging`) on the PR **before**
the merge — the target repo's promote workflow reads the labels of an already merged PR. If the label
did not get applied, the merge is not performed; if the merge is rejected, the label is removed: an
orphaned label would turn a later plain Merge into a silent deploy. Details —
[[decisions/0007-promote-label-and-base-branch]].

## The base branch

`base_branch` in `.loop.yml` is read **from the repository's default branch**
(`loopconfig.resolve_base_branch`) — an override cannot tell you where to look for itself. The fork
point (`scheduler.bootstrap`) and the PR base (`_publish_plan`) must match: forking from `main` while
the base is `staging` would produce a diff containing everything `staging` is missing.

## Links

- [[components/pipeline]] · [[components/clients]] · [[components/ingress-and-control]]
- [[concepts/sandboxd-platform]] — where the constraint came from
- [[decisions/0006-merge-gate-and-conflict-resolver]] · [[decisions/0007-promote-label-and-base-branch]]
