# Wiki Log — chronological journal

Append-only. Entry: `## [YYYY-MM-DD] <type> | <title>` + 1–3 lines.
`<type>` ∈ `bootstrap` · `ingest` · `decision` · `probe` · `smoke` · `incident` · `query` · `lint`.
Format and rules — [[conventions]] §5. New entries go **on top**.

---

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
