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

### What a clean merge still hides

Green check runs are a weaker signal than they look, and the resolver prompt now says so. Two branches
that each added "the next" sequentially numbered artefact — a database migration, a numbered decision
record — end up with **different filenames**, so git merges them without a marker while the result is
broken: a forked migration graph, two documents claiming one number. No CI job runs migrations, so the
gate above sees green and merges it. `build_sync_prompt` makes the resolver hunt for that class after
the merge (one head, renumber its own side, re-derive lockfiles with their tool, take `.loop/` from the
base, run the repo's own checks on the merged tree).

**Staleness is computed, not trusted** (2026-08-07). `mergeable_state == "behind"` arrives only while the
base carries a strict "up to date" rule; the work repos dropped it, so a stale branch reads as `clean`.
`_merge_readiness` therefore asks `compare/{base}...{head}` for `behind_by` and reuses the existing
`behind` branch — `> 0` updates the branch and waits for the re-run, `== 0` merges on the first press
(an empty merge commit is itself a push worth a full CI run). The check sits immediately before the
merge, so the window in which a rival can land is seconds wide.

**The promote path does not run on the merge event.** `promote-staging.yml` triggers on
`workflow_run: workflows: ["ci"], branches: [main], types: [completed]` and runs only when that run
concluded `success`; it promotes `workflow_run.head_sha`. So our button's chain is merge → `ci` on
`main` → success → promote, and the workflow's own "no successful `ci` run" guard is a second belt on
a path that already satisfies it. The failure seen on 2026-08-06 was the `workflow_dispatch` branch of
the same workflow, where the target is whatever `main` points at right now — freshly merged, `ci` still
running. **Load-bearing consequence:** the merge must not be made with an Actions `GITHUB_TOKEN`, or no
`ci` run starts on `main` and the promotion never fires. The orchestrator merges with its own PAT, which
is what keeps this working — do not "simplify" that token.

**Detector and preventer are different halves.** Freshness alone does not stop a forked migration graph —
a PR that is current when checked still forks it if a rival merges afterwards. The backend `gates` job
now checks "exactly one head" and unique decision numbers, but on a PR it runs against the merge-ref
where the head is honestly single: it fires on `push: main`, *after* both sides merged. That protects the
rollout (promote-staging refuses a commit without a green `ci`) without protecting `main`. The two halves
compose: `behind_by > 0` → update-branch → both migrations sit in one tree **before** the merge → the
gate reddens the PR instead of `main`. Neither half does that alone.

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
