# Wiki Log — chronological journal

Append-only. Entry: `## [YYYY-MM-DD] <type> | <title>` + 1–3 lines.
`<type>` ∈ `bootstrap` · `ingest` · `decision` · `probe` · `smoke` · `incident` · `query` · `lint`.
Format and rules — [[conventions]] §5. New entries go **on top**.

---

## [2026-08-10] ingest | `pipeline.py` became a package, one module per stage

1773 lines and ~40 methods in a single class had become the file every feature touched and no reader
could hold. Split into `src/loop_orchestrator/pipeline/`: `core` (the `Pipeline` class and `process`,
the only long method left — it is where the stage order is written down), one module per stage
(`prepare`, `execute`, `review_stage`, `e2e_stage`, `contract_stage`, `publish`, `preview`,
`planning_stage`, `gitsync`, `reporting`), the platform-facing `sandbox_tasks` and `tracing_mixin`
underneath, and the leaves `errors` / `constants` / `clock`. Each stage is a mixin; `core.Pipeline`
inherits all of them, so no call site changed. Behaviour is identical — the one deliberate
de-duplication is how a failed task is classified (`constants.failure_blob`/`is_rate_limited`/
`is_transient`), which the two polling loops had verbatim twice. `pipeline/__init__.py` re-exports the
old surface, so `from loop_orchestrator.pipeline import …` still works everywhere. Two test patch
targets moved with the code: the fake clock now patches `pipeline.clock.monotonic` (which is why the
stages call `clock.monotonic()` through the module rather than binding the name) and
`PREVIEW_READY_TIMEOUT_S` now lives in `pipeline.preview`. 574 tests pass unchanged otherwise.
Pages: [[components/pipeline]].

## [2026-08-10] ingest | A dependency now carries the upstream API contract, not just "wait"

A blocked issue used to inherit one bit from its blocker, so the consumer's planning agent invented
the producer's API. The run that motivated this: a two-repository connector feature whose backend
issue shipped a real ingest API while the frontend issue, planned in ignorance of it, was written
against endpoints that never existed — the mismatch surfaced only when the frontend code ran. Shipped: a `contracting` stage between
`e2e_testing` and `staging` (fresh session, `LOOP_CONTRACT_MODEL`, JSON verdict, `none` is a result,
never fatal), the `upstream_contracts` table plus `issue_tasks.depends_on` (because `blocked_by`
forgets a dependency exactly when the blocker closes), a `<!-- loop:api-contract -->` issue comment a
human can edit and which then outranks the stored row, delivery as the `## Upstream dependencies`
section of `.loop/task.md` plus `.loop/context/<repo>/<path>` (≤10 files, 256 KiB), and planner and
Advisor gates that turn an untraceable endpoint into questions or `revise`. Open Question 1 is
resolved: both `GET /repos/{repo}/issues/{n}/dependencies/blocked_by` and `.../blocking` were probed
live on 2026-08-10 and answer HTTP 200 — no fallback scan was needed. Spec:
[`docs/superpowers/specs/2026-08-08-upstream-api-contract-handoff-design.md`](../superpowers/specs/2026-08-08-upstream-api-contract-handoff-design.md);
plan: [`docs/superpowers/plans/2026-08-08-upstream-api-contract-handoff.md`](../superpowers/plans/2026-08-08-upstream-api-contract-handoff.md).
Pages: [[concepts/contract-handoff]] · [[concepts/run-lifecycle]] · [[concepts/agent-steering]] ·
[[components/pipeline]] · [[components/storage-and-config]] · [[components/worker-and-scheduler]] ·
[[overview]]. The live two-repository check is still pending.

## [2026-08-08] ingest | Planning became a per-repository setting

`.loop.yml` gains a `planning:` section: `enabled`, `model`, and `advisor.{enabled,model,max_iterations}`.
Each model falls back to the `LOOP_*` setting it overrides, `advisor.enabled: false` publishes the
first plan with no review round (recorded as a `run_events` line, so a thin plan can be told from one
an advisor waved through), and `enabled: false` stops the scheduler opening planning Runs for the repo
at all — its issues simply stay in the backlog. Two design points worth keeping: `planning.enabled` is
read from the **default** branch (`loopconfig.planning_enabled`), because the decision precedes the
issue branch, and it is fail-safe on — a missing, broken or unreachable config must not silently stall
a backlog; and everything else is snapshotted onto the Run at `preparing`, so a config edited mid-Run
cannot change the rules that Run started under. The scheduler asks only when it actually has a
candidate, so an idle tick costs no extra GitHub calls. 18 tests.
Pages: [[components/storage-and-config]] · [[concepts/agent-steering]] · [[components/pipeline]] ·
the `.loop.yml` Locked Decision in [the MVP spec](../superpowers/specs/2026-07-31-loop-engineering-mvp-design.md)
now states that the schema only ever grows.

## [2026-08-08] decision | The paused sandbox sleeps; its preview link wakes it

The pause held a live sandbox for two hours — ~3.5 GB and a dev server **outside**
`LOOP_MAX_CONCURRENT_RUNS`, so a cap of 3 could mean 5–6 live containers. The blocker was never the
stop but the comeback: our preview server is started with `nohup` through `exec` and dies with its
container. A probe (see [[concepts/sandboxd-platform]], 2026-08-08) settled both halves — traefik's
`sandbox-wake` catch-all at priority 1 starts a stopped sandbox on the first hit of its preview host,
and a server declared in `sandbox.yaml` is restarted by runtimed while an `exec`-started one is not.
So `_arm_preview_manifest` writes that manifest (only *after* the port answered, only when the repo
does not track its own, and into `.git/info/exclude` so no revise commit picks it up), `_sleep_pause`
stops the sandbox after the videos are out, and `revise` wakes it. No manifest → the old awake pause,
unchanged. First open after a sleep takes ~10 s and can 502 once; the approval message says so.
10 tests. Pages: [[decisions/0015-sleep-the-paused-sandbox]] · [[concepts/sandboxd-platform]] ·
[[concepts/run-lifecycle]] · [[components/pipeline]] · [[overview]].

## [2026-08-08] incident | The prune cron ate the sandbox images again, and a Run waited three hours

The nightly `docker image prune -af` was recreated **with the original `-af`** when the stack moved
hosts on 2026-08-06, the anchor containers were not, and at 03:35 UTC it deleted `loop-sandbox:latest`
and `sandboxd-base:0.3.0`. The first Run afterwards (planning, `<frontend-repo>`) failed its workspace
seed — `Unable to find image 'loop-sandbox:latest' locally` — and sandboxd left the sandbox in status
`error`. Nothing downstream reads that: `create_sandbox` adopted the stillborn sandbox on the retry's
409, and `_submit_resumable` read every following 409 as "busy" and resubmitted every 20 s. 53 minutes
of it, no `run_events`, no Telegram, and three hours of `timeout_minutes` ahead.
Host fixed (cron `-af` → `-f`, both images rebuilt, `keep-*` anchors created, image skills verified);
code fixed on both sides — a sandbox in `error` is no longer adopted and no longer waited out
(`_sandbox_is_dead`). Pages: [[ops/vps]] · [[concepts/resilience]] §6 · [[components/pipeline]] ·
[[overview]].

## [2026-08-08] ingest | The planner's revise round stopped inheriting the advisor's session

`_planning` passed `continue_session=iteration > 0` with a comment claiming the planner kept its own
session across revise rounds. It did not: sandboxd can only resume *the most recent* session, and the
advisor runs between the rounds — so the planner was editing its documents from the reviewer's
context. Every round is now fresh and `build_planner_revise_prompt` is self-contained (task file, both
document paths, lockfile and push rules). Resuming the planner by session id was considered and
dropped: the v1 task API takes only `Continue *bool`, and an advisor round outlives the five-minute
prompt cache anyway, so the "cheap" resume would arrive at write price.
Pages: [[decisions/0013-one-session-per-stage]] · [[components/pipeline]].

## [2026-08-08] ingest | The merge buttons became an indicator of the gate

Consequence of the entry below: with CI on a two-slot self-hosted pool, pressing Merge and being told
"checks are still running" became the normal way to use the button. Telegram has no disabled button, so
the keyboard is redrawn instead — `⏳ CI 2/3` · `🔴 CI red: image` · `⤴️ Update branch` ·
`🔧 Resolve & merge` · and the plain merge pair only when the gate would accept it. The reaper repaints
every 60 s from the new public `Actions.gate`; `_merge_readiness` now returns a `Gate` named tuple whose
`done`/`total` label the button and never touch the decision. `notify_done` returns its message id into
the new `runs.tg_merge_message_id` so there is something to repaint.
Two decisions worth keeping: the repaint is **unconditional** (a memo of the last-drawn keyboard goes
stale the moment `_run_action` clears the buttons after a press, and Telegram's "not modified" is a
normal answer anyway), and `⤴️ Update branch` is its **own** action — a Merge press that silently
updated instead would merge if the gate turned clean between the repaint and the press.
**Driven by the `check_run` webhook, not by polling.** Both target repos' hooks were subscribed to
`check_run` alongside the three events they already carried (a classic `repo` scope edits them;
`PATCH .../hooks/{id}` replaces the event list wholesale, so send all four). `_spawn_repaint` resolves
the PR from `check_run.pull_requests[]` and repaints within seconds; the 60 s reaper sweep stayed on as
the net for a dropped delivery or one that arrived mid-restart. 14 tests
([[components/ingress-and-control]]).

## [2026-08-08] ingest | The target repos' CI moved to a self-hosted pool; our gate needed no change

Backend ADR 0076: the org ran out of GitHub-hosted minutes, so every job in both repos became
`runs-on: ${{ vars.CI_RUNNER }}` (label `ssc-build`, two ephemeral runners on the **old** 2-core VPS the
loop stack left on 2026-08-06 — our 4 vCPU box is untouched, and `hbs-1991/claude-loop-swe` bills its own
minutes). Self-hosted minutes are not billed, which is what un-blocked the buttons after the payment
suspension. The plan shrank `ci` to `gates` alone; a day later `tests-selective` and `image`+Trivy came
back (PRs #31, #32), so `main` reports three checks and the ruleset requires exactly those three.
`tests-full` stayed deleted (~25 min on two cores) and the coverage floor with it.
**Verified against our code: nothing to change.** `required_checks` reads the ruleset per press, so the
rebuilt check list arrived for free — the payoff for never hardcoding it. What did change is cost and
failure mode: two slots shared by both repos make `checks_pending` last longer and make a
`behind_by > 0` re-run expensive, and the runner box is now a single point of failure that presents as
"checks are still running" forever ([[ops/target-repos]]).
**Corrected a diagnosis from the thread:** `promote-staging` fires on `workflow_run` of a *successful*
`ci` on `main`, not on the merge event, so our button does not hit the guard that failed on 2026-08-06 —
that was the `workflow_dispatch` path. The real dependency is that the merge uses our PAT: an Actions
`GITHUB_TOKEN` merge starts no `ci` run on `main` and the promotion would silently never happen
([[concepts/publication]]). Both `auto-merge.yml` guards for loop PRs are in place and documented on
their side.
**Obsolete on their side:** the `create-pr` skill still describes four jobs, an 18-minute `tests-full`
to read before merging, and per-minute billing — none of which exist now.

## [2026-08-07] ingest | Staleness is computed now that the repos dropped the strict rule

The work repos turned off "require branches to be up to date" (it forced a full CI re-run on every open
PR; merge queue, the cheap equivalent, is rejected `422 Invalid rule` on their org plan). Consequence:
GitHub stopped sending `mergeable_state: "behind"`, the branch in `_merge_readiness` went dead, and a
stale branch started arriving as `clean` with green checks that measured a tree the merge would discard.
`GitHubClient.behind_by` reads `compare/{base}...{head}` and the gate reuses the existing `behind`
path — `> 0` updates and waits, `== 0` merges on the first press. 4 tests
([[concepts/publication]], [[decisions/0006-merge-gate-and-conflict-resolver]]).
**Correction to the previous entry:** the forked migration graph is not a consequence of dropping
strict, and freshness alone does not prevent it — a PR current at check time still forks the graph if a
rival merges after. The backend's new `gates` checks (one head, unique decision numbers) are a *detector*
firing on `push: main`; our `behind_by` is what turns the pair into prevention, by putting both
migrations in one tree while the PR is still the thing that can go red.
**Blocked meanwhile:** GitHub Actions are suspended on the org for a failed payment (~08:21 UTC
2026-08-07). Until that is cleared no `ci` runs, so the merge gate sees permanently pending checks and
every merge/promote button refuses — independent of any code above.

## [2026-08-07] ingest | The resolver prompt now names the hazards git merges without a conflict

A target repo shipped a `create-pr` skill, which raised two questions. **Reach:** the resolver is the
only loop agent whose task matches such a skill's triggers ("resolve the PR conflicts", "update the
branch") — the executor and the planner never create a PR (the orchestrator does, `_publish_plan`) and
cannot fetch. **Content:** its steps 3–5 describe a real gap of ours. Two branches that each add "the
next" numbered artefact merge **with no conflict marker at all** — a second Alembic head, a duplicate
decision number — and no CI job runs migrations, so our merge gate reads green check runs and merges a
forked graph. `build_sync_prompt` now names that class generically (numbered artefacts, one head,
renumber your side), keeps lockfiles on their generating tool, takes `.loop/` from the base, asks for
the repo's own checks on the merged tree, and explicitly cancels the skill's push + `gh pr create`
ending — neither is possible in a sandbox. 2 tests; the prompt had none before
([[concepts/agent-steering]], [[concepts/publication]]).
**Still open:** `mergeable_state == "behind"` is only reported when the base is protected, and the
production repos have no branch protection — a stale branch reaches us as `clean`, so the CI gate that
would catch the second head never re-runs. Fixing that means computing `behind_by` ourselves.

## [2026-08-06] ingest | A sandbox finally has a CPU ceiling, and a stale mirror row no longer holds a lane

Two holes closed after the move. **Ceilings:** a local patch to sandboxd adds `CPUs` to its
`docker.RunSpec` and moves all three limits into `SANDBOXD_CPUS`/`SANDBOXD_MEMORY`/
`SANDBOXD_MEMORY_SWAP`, defaulting to the old literals so the diff is inert elsewhere. We run 3 cores
/ 5 GB / 7 GB swap-total on a 4-core box — one Run may take three cores but never the fourth, which is
exactly what starved on 2026-08-05. Verified: `NanoCpus=3000000000`, `Memory=5368709120`,
`MemorySwap=7516192768`. The patch lives in `deploy/sandboxd-patches/` because **a platform upgrade
drops it silently** ([[concepts/sandboxd-platform]], [[concepts/resilience]]).
**Lane deadlock:** `_resolve_running` judged the run the mirror pointed at, and a mirror left on a
finished *planning* run matched no branch — issue #10 held lane `gbp` for a day while its PR was
already merged, so restoring two issues to the backlog started nothing. It now recovers the
planning→PR handoff from the runs table ([[components/worker-and-scheduler]], 2 tests).
Also measured: a single planner takes **319% CPU** when four cores are free — the "one core per Run"
figure was a contention artefact of the two-core box, not an appetite.

## [2026-08-06] ingest | The stack moved to a 4 vCPU / 16 GB host

[[decisions/0012-one-bigger-host-over-a-multi-host-pool]] executed: fresh Ubuntu 24.04, 4 GB swap,
`LOOP_MAX_CONCURRENT_RUNS=3`. The migration carried `~/.sandboxd/data/{agent-auth,secrets.key,state}`
and `~/loop/{.env,data,secrets}`, so **the Claude OAuth, the sandboxd API key and the git credential
all survived** — `.env` needed no edit beyond the concurrency cap. Verified end to end on the new box:
a private repo cloned through the migrated credential, a sandbox came up, and an agent task answered
on the migrated OAuth.
Procedure, the three things that bite and the cutover order — [[ops/vps]]. The sharpest one: Hostinger
applies an API-attached SSH key **only at provisioning**, so a machine with no other access needs a
recreate to let you in. Old host keeps its unrelated neighbour service; the loop stack there is
stopped.

## [2026-08-06] smoke | Tracing verified end to end on a live Run (#53, smoke repo)

Full tree in Jaeger: 168 spans, depth 0-4 (`run` -> 4x `stage` -> 4x `agent.session` -> 76x
`api.call` -> 83x `tool.*`), **zero orphaned parents**. The derived span ids hold: stages emitted
minutes apart still attach to one root. First real numbers — executing $1.17 / 26 calls, review
**$0.98 / 7 calls**, review-fix $0.56 / 13, e2e $1.75 / 30; total $4.46. The reviewer costs
**~$0.14 a call against the executor's $0.045** — Fable at twice Opus per token, now visible instead
of argued. `session.opening_context_tokens = 38,532` on a fresh executor session confirms
[[decisions/0013-one-session-per-stage]] in production (it was ~230k inherited before).
Two deploy failures on the way, both mine: a Jaeger tag that does not exist (`1.62`; tags are
three-part) and a named volume mounted at a path absent from the image, so it came up root-owned and
badger died as uid 10001 — `user: "0:0"`. Page: [[components/tracing]].

## [2026-08-06] ingest | Agent tracing: a Run's cost and context, span by span, in Jaeger

`tracing/` + `clients/otlp.py`: after every agent task the session JSONL is copied out of the
sandbox (`exec_cmd` — the files API is rooted at `workspace/app` and cannot see `$HOME/.claude`)
and turned into `run -> stage -> agent.session -> api.call -> tool.*`. Records what a fresh session
opens with, what each call adds to the context, which tool added it, cache misses, idle gaps past
the 5-minute TTL, and dollars at every level. Off unless `LOOP_OTLP_ENDPOINT` is set; never fails a
Run; previews capped at 500 chars with project secrets scrubbed **before** truncation. No new
dependencies — [[decisions/0014-hand-rolled-otlp-emitter]]. Page: [[components/tracing]].
Spec/plan: `docs/superpowers/{specs,plans}/2026-08-06-agent-tracing-otel.md`. 470 tests green.

## [2026-08-06] decision | Every stage gets its own Claude session; 61% of the bill was cache writes

Profiling two live Runs showed `submit_task` never sent `continue`, so sandboxd's "resume if a
session exists" default applied and the reviewer opened every call on ~230k tokens of the executor's
context — re-billed at write price because the model switch missed the cache. Tri-state `continue`,
stated by every caller; the prompts now carry what the inherited session used to. Two behavioural
bugs fell out: the advisor was reviewing plans inside the planner's session, and `revise` lost the
executor's session it had been reaching by accident. [[decisions/0013-one-session-per-stage]],
[[components/pipeline]].

## [2026-08-06] ingest | The preview server starts through `exec`, not through an agent task
`_start_preview` no longer submits a Claude Code task to type `npm run dev`: `build_preview_script`
builds a shell script (sources `.loop/secrets.env`, exports the e2e env, `nohup`s the command) and
`SandboxdClient.exec_cmd` runs it — a round trip instead of a model call and ~40 s. The URL is now
published only after `_preview_responds` sees the port answer, and a failure keeps the tail of
`.loop/preview.log` in `run_events` before the app takes the log with it.
`build_preview_prompt` deleted. Script syntax verified under the sandbox image's own `/bin/sh`.
Pages: [[components/pipeline]], [[components/clients]]. 395 tests green.

## [2026-08-06] probe | A sandbox survives stop/start with its preview route intact
Blank app → sandbox on port 3000 → toy server via `exec` → `stop` → `start`: `preview.url` comes back
byte-identical, Traefik answers **502** immediately after start (route whole, upstream missing) and
200 once the server is respawned. `stop` 0.4 s · `start` 0.3 s · respawn→200 2.2 s. Processes do not
survive, so the dev server must be started again — one `exec` call, not an agent task.
Unblocks sleeping the sandbox of a Run paused in `awaiting_approval`; recorded in
[[concepts/sandboxd-platform]]. Two side findings there too: `POST /sandbox/{id}/exec` runs commands
without the agent, and the host-side `127.0.0.1:9090` does serve `/v1` (the old "returns 400" note was
stale — a probe can now be a plain curl script).

## [2026-08-06] decision | VPS capacity measured; moving to one bigger host
`docker stats` under two live Runs: each sandbox takes a **whole core and ~3.0–3.5 GB**, while the
entire control plane costs 0.22% of a core and 270 MB — so the host is sized purely by how many Runs
must fit, and the rule is `(N+1)` vCPU / `(3.5·N + 2)` GB ([[ops/vps]]). Chose a 4 vCPU / 16 GB host
over a two-host pool and over commercial per-second sandboxes —
[[decisions/0012-one-bigger-host-over-a-multi-host-pool]].
From the sources: sandboxd hardcodes `CPUShares: 100` / `Memory: "10g"` / `MemorySwap: "10g"`, has no
`--cpus` support at all and disables container swap — so no code-side lever exists over sandbox CPU
([[concepts/sandboxd-platform]], [[concepts/resilience]]). `runtime_preset` is a framework, not a size.

## [2026-08-06] decision | Environment specifics scrubbed out of the documents
Host address, webhook/preview domain, GitHub owner and org, smoke and production repository names,
the sibling project on the same VPS and an absolute Windows path are now placeholders (`<vps-ip>`,
`loop.example.com`, `<org>`, `<backend-repo>`, …) across 12 documents; the real values live only in
`~/loop/.env` and `~/.sandboxd/src/.env` on the host.
Rule recorded in `CLAUDE.md` §Conventions and [[conventions]] §1, rationale in
[[decisions/0011-no-environment-specifics-in-the-repo]]; [[ops/vps]] and [[ops/target-repos]] carry a
note so a reader does not mistake a placeholder for a literal.
**Still open before the repo goes public:** git history holds the pre-sanitisation versions.

## [2026-08-06] decision | The whole documentation set moved to English ahead of open-sourcing
Every document in the repository is now English — 50 files, ~15,100 lines (6 specs, 6 plans, the
whole wiki, `CLAUDE.md`, `docs/deploy.md`, the skills and hook texts), translated by a 16-stream agent
workflow with a per-stream structural verification against the Russian snapshot (heading order, code
fences, checkbox states, table rows, links: zero drift).
The rule now lives in `CLAUDE.md` §Conventions, [[conventions]] and Locked Decision 9 of the
reviewer-phase2 spec; the document carve-out of [[decisions/0008-english-everywhere]] is withdrawn by
[[decisions/0010-documentation-in-english]]. Russian survives only in the live conversation with the
user. Kept on purpose: the Cyrillic fixtures in the Telegram formatter tests, now non-ASCII
regression coverage.

## [2026-08-06] bootstrap | LLM wiki created and filled with the knowledge of five phases
The `docs/wiki/` layer was created (Karpathy pattern): schema [[conventions]], catalogue [[index]],
state [[overview]], sections `concepts/` `components/` `ops/` `decisions/`; the machinery — the
`SessionStart`/`Stop` hooks and the `/wiki-ingest`, `/wiki-lint` skills ([[decisions/0001-llm-wiki-memory-system]]).
The initial ingest moved into the wiki the knowledge that had been sitting in the agent's personal
memory as a single file: sandboxd behaviour, the incidents that were analysed, ops knowledge about
the VPS and the target repositories, nine implementation decisions.

## [2026-08-05] decision | Secrets reach the sandbox as a file — app config never gets to the agent
A probe and the sandboxd sources showed: neither `sensitive: true` nor a plain config variable ever
appears in the agent's environment (the `access_policy` broker does not exist in the code,
`agentenv.go` strips everything `*_TOKEN`/`*_KEY`/`*_SECRET`). The phase 5 smoke test was a false
positive — the smoke repo is public. Now `.loop/secrets.env` + `.loop/.gitignore` via the files API,
and prompts name only the key names ([[decisions/0002-secrets-as-file]], [[concepts/secrets-delivery]], commit `8551f45`).

## [2026-08-05] incident | VPS overload broke Docker DNS and killed two runs
Three parallel runs → load average 19 on two cores → dockerd stops answering the 127.0.0.11 resolver
in time → `Temporary failure in name resolution`; the sandboxes of the crashed runs stayed alive and
burned the quota for another half hour. Fix: polling tolerates transport failures,
`LOOP_MAX_CONCURRENT_RUNS=2` ([[decisions/0009-concurrency-cap-and-poll-resilience]], [[concepts/resilience]], commit `25b02d6`).

## [2026-08-05] decision | Merge is gated on CI, conflicts are resolved by a background agent
A red PR#13 got merged and carried a broken `uv.lock` into main (production repos have no branch
protection); the source of the breakage was the planner committing the lockfile. Now
`_merge_readiness` reads check runs, `behind` is cured by `update-branch`, a conflict goes to a
resolver agent with a temporary `GIT_SYNC_TOKEN`
([[decisions/0006-merge-gate-and-conflict-resolver]], commits `5069e54`, `91a8d5e`).

## [2026-08-04] probe | Image skills never reached the agent — the sandbox home is shadowed by a mount
`/home/sandbox` is shadowed by a per-sandbox loopback workspace; the home is seeded from
`/opt/sandbox-skel`. Which means the `playwright-cli` skill from phase 3 never worked at all (e2e
rode on the prompt and the global CLI). The skills moved, and a post-build image check was added
([[decisions/0004-skills-into-sandbox-skel]], [[ops/sandbox-image]], commit `2da2ce8`).

## [2026-08-04] probe | A hidden ~35 minute ceiling per Run: the sandboxd idle reaper
`last_active_at` is bumped only by sandbox creation and the exec endpoints, not by the async task
API, so the reaper stopped the sandbox in the middle of the agent's work. Fix — a keepalive on every
poll tick plus a separate window for the approval pause
([[decisions/0003-keepalive-against-idle-reaper]], commit `e424cca`).

## [2026-08-04] smoke | Two-repository scenario passed end to end
A backend issue blocked a frontend issue through a native cross-repo dependency; closing the backend
unblocked the frontend (picked up by the poller, not the webhook), the frontend planner read the API
contract on its own, and e2e brought both services up in one sandbox — 8 scenarios green ([[ops/target-repos]]).

## [2026-08-04] incident | Cron was wiping sandbox images off the VPS
`/etc/cron.d/docker-image-prune` with `-af` deleted `loop-sandbox`/`sandboxd-base`: the seed uses
them ephemerally, so between runs they look "unused". Fixed to `-f` plus anchor stopped containers;
the change was made without root — via `docker run --user 0` ([[ops/vps]]).
