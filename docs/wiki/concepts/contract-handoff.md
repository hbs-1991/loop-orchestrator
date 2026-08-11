# Concept: the upstream API contract handoff

A dependency between two backlog issues used to carry exactly one bit — *wait*. When the blocker
closed, the dependent task started and its planning agent knew nothing about what the blocker had
actually built. In a two-repository feature that is fatal: the backend task ships a real API, the
frontend task is planned against an imagined one, and the mismatch surfaces only when the frontend
code runs. A guessed endpoint reads plausibly, passes the Advisor and dies in production. The handoff
gives the dependency a payload: the producing Run describes the interface it built, the orchestrator
stores and publishes that description, and every dependent task is planned against it **plus** the
authoritative source files — with the planner forbidden to invent anything it cannot trace to one of
the three permitted sources.

Rationale and every Locked Decision live in the spec, not here:
[`docs/superpowers/specs/2026-08-08-upstream-api-contract-handoff-design.md`](../../superpowers/specs/2026-08-08-upstream-api-contract-handoff-design.md).

## What the stage captures

`contracting` is a stage of the **execution** Run, between `e2e_testing` and `staging`
([[concepts/run-lifecycle]]). `_prepare` sets `run.contract_enabled = run.issue_number is not None` —
only a Run tied to an issue can hand anything downstream — and the stage itself then asks
`gh.issue_blocking(repo, issue_number)`. Empty list (or a lookup failure) → `contract_status =
"skipped"`, straight on to `staging`. The decision is deliberately made at stage time, not at prepare
time: a dependency can be added hours after the Run started.

When the issue does block someone, `Pipeline._contracting` runs **one agent task in the already-live
sandbox** — no new app, no new clone. Two details matter:

- **Fresh session** (`continue_session=False`). An agent that inherits the executor's session inherits
  its *belief* about what it built; `build_contract_prompt(head_branch)` instead names
  `git diff origin/<head_branch>..HEAD` so the description is read off the code
  ([[decisions/0013-one-session-per-stage]]).
- **`LOOP_CONTRACT_MODEL`**, default `claude-sonnet-5` — the work is reading a diff and transcribing
  it, not reviewer-grade judgement.

The verdict is the usual JSON-in-the-final-message protocol ([[concepts/agent-steering]] §5), parsed
by `contracts.parse_contract_output` through `jsonextract.find_json_object`, with four fields:
`outcome` (`contract` | `none`), `contract` (markdown), `sources` (≤10 repo-relative paths, capped and
de-slashed at parse time) and `breaking_changes`.

**`outcome: "none"` is a result, not a failure** — it still writes a row with an empty
`contract_md`. "The blocker shipped nothing consumable" is information; a missing row is
indistinguishable from a stage that never ran.

`contract_status` therefore has four values: `produced` · `none` · `skipped` · `failed`. Everything
except `produced`/`none` skips the issue comment, and **nothing here is fatal** — the work is already
executed and reviewed, so any failure records its reason in `run_events` and proceeds to `staging`.

Where the captured contract goes:

| Sink | Written at | Purpose |
|---|---|---|
| `upstream_contracts` row, keyed `(repo, issue_number)` | `contracting` | what consumers resolve — [[components/storage-and-config]] |
| `run.contract_json` | `contracting` | so Telegram can render the text before the issue comment exists |
| the approval message, in an expandable blockquote | `awaiting_approval` | the only moment a wrong contract is cheap to reject |
| the `<!-- loop:api-contract -->` issue comment | `publishing`, via `_publish_contract_comment` | the copy a human can correct |

The comment is written by `gh.upsert_marked_comment`: one marked comment per issue, edited by its
marker on a re-run rather than appended to.

## Resolution order at the consumer

`contracts.collect_upstreams(db, gh, task)` builds one `Upstream` per entry of
`issue_tasks.depends_on`, taking the best available source for each field:

1. **Contract text** — the marked issue comment (`gh.find_comment` + `extract_contract`), because a
   human may have corrected it; otherwise the stored `contract_md`.
2. **Sources, PR number** — always the stored row; the comment is prose, the row is structure.
3. **PR number fallback** — `dbmod.latest_run_for_issue(repo, number, "pr")` when no row exists, so a
   dependency with no contract at all still points at the code that closed it.
4. **Title** — `gh.get_issue`, best-effort; it is a heading, not a requirement.

**The section renders even when none of them resolved.** A dependency with nothing recorded still
yields an `Upstream`, and `render_upstream_section` prints "No contract digest was captured for this
dependency. Do not guess its interface: read its code if it is reachable, otherwise ask." Silence is
what produced invented endpoints in the first place — it reads identically to "there was no upstream".

## The two delivery channels

| Channel | Written by | Committed? | Role |
|---|---|---|---|
| `.loop/task.md` → `## Upstream dependencies` | `scheduler.bootstrap` → `planning.build_task_file` | **yes**, to the issue branch | the readable digest, at planning altitude |
| `.loop/context/<repo>/<path>` + `README.md` | `Pipeline._write_context` → `contracts.fetch_context_files` | **no**, uploaded into the sandbox | the authority — where digest and file disagree, the file wins |

The digest is committed and survives everything; the files are uploaded through the same
`sb.put_file` path that already places `.loop/secrets.env` ([[concepts/secrets-delivery]]), from both
`_prepare` and `_prepare_planning`, so the executor gets them as well as the planner.

`.loop/.gitignore` (`*`) is what keeps another repository's source files out of the consumer's
commits, and `_write_context` writes it **itself** rather than relying on `_write_secrets` — a
consumer repo with no secrets at all would otherwise have no gitignore and would commit the whole
upstream tree into its own PR.

Limits: **≤10 source paths per upstream** (`MAX_SOURCES`, enforced twice — at parse time and at fetch
time) and **256 KiB of context in total** across all upstreams (`MAX_CONTEXT_BYTES`). What does not
fit is dropped and named in `.loop/context/README.md`; a producer that listed its whole `src/` would
otherwise eat the consumer planner's context exactly where it is needed for planning.

Both prompts gate on the same three sources — code in this repository, `.loop/context/`, and the
`## Upstream dependencies` section: `planning.build_planner_prompt` routes anything else to
`outcome: "questions"` (the existing needs-info dialogue, no new state), and
`build_advisor_prompt` demands every endpoint, field name and status code be *traceable* to one of the
three, else `revise` ([[concepts/agent-steering]] §1).

## Gotchas

- **Both native dependency endpoints exist.** `GET /repos/{repo}/issues/{n}/dependencies/blocked_by`
  and `.../blocking` were probed live on **2026-08-10** and both answer HTTP 200 with a JSON array —
  the planned fallback scan over `LOOP_BACKLOG_REPOS` was never needed and is not implemented. 404/410
  is still read as "none" so repositories without the feature keep working. Do not re-probe; the
  finding is repeated in the `_dependencies` docstring.
- **`blocked_by` forgets a dependency at the exact moment it becomes useful.** It means *open*
  blockers, because `_launch_ready` gates on it, so it empties when the blocker closes — which is when
  the consumer starts and needs to know who its producer was. Hence the second column,
  `issue_tasks.depends_on`, holding every dependency as `{repo, number}` regardless of state. One
  `issue_dependencies` call in `_sync` fills both ([[components/worker-and-scheduler]]).
- **Sources are read from the producer's base branch, not from the captured sha.** The consumer must
  be planned against what is in the trunk when it starts. A path that has since been renamed or
  deleted becomes a `missing_source_note` file rather than a silent absence — a promised file that
  vanishes without trace is how a planner talks itself into guessing.
- **The stage sits before the approval pause on purpose.** The `awaiting_approval → executing` revise
  path replays the chain, so amended code re-derives its contract automatically and the row is
  overwritten (`(repo, issue_number)` is UNIQUE, latest write wins, no history). A stage after
  `publishing` could not do that.
- **The comment outranks the row unconditionally.** That is the point — a human edit must not be
  silently outvoted — but it also means emptying the text between the markers erases the digest for
  every consumer, while the `sources` list keeps coming from the row.
- **A `contracting` Run is restartable.** `Worker.recover` re-enqueues it: the stage re-captures and
  overwrites the row it keys, so a restart mid-capture costs one model call, not a wrong contract.

## Links

- [[concepts/run-lifecycle]] (the state) · [[components/pipeline]] (`_contracting`,
  `_publish_contract_comment`, `_write_context`) · [[components/storage-and-config]] (the table and the
  columns) · [[components/worker-and-scheduler]] (`depends_on`, `collect_upstreams` in `_launch_ready`)
- [[concepts/publication]] — the two-phase publication the comment rides on ·
  [[concepts/agent-steering]] — the prompt, the model and the JSON verdict ·
  [[concepts/secrets-delivery]] — the `put_file` path `.loop/context/` reuses
- Spec: [`2026-08-08-upstream-api-contract-handoff-design.md`](../../superpowers/specs/2026-08-08-upstream-api-contract-handoff-design.md) ·
  plan: [`2026-08-08-upstream-api-contract-handoff.md`](../../superpowers/plans/2026-08-08-upstream-api-contract-handoff.md)
