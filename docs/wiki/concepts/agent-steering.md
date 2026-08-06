# Concept: how we steer the agent inside the sandbox

The orchestrator does not write code — it **hands tasks to the agent** and parses its answers. There are
exactly five channels of influence, and it helps to remember where each of them lives.

## 1. The stage prompt

Assembled in code: `pipeline.build_prompt` (executor), `review.build_review_prompt` /
`build_fix_prompt`, `e2e.build_e2e_prompt` / `build_e2e_fix_prompt`, `planning.build_planner_prompt` /
`build_planner_revise_prompt` / `build_advisor_prompt`, `pipeline.build_sync_prompt` /
`build_preview_prompt`. All of them in English (project convention).

The prompt names only the secret key names and the load line — see [[concepts/secrets-delivery]].

## 2. The model per stage

`LOOP_REVIEWER_MODEL` (default `claude-fable-5`), `LOOP_ADVISOR_MODEL` (`claude-fable-5`),
`LOOP_E2E_MODEL` and `LOOP_PLANNER_MODEL` (empty = the executor's model). The `model` field is only
accepted by the sandboxd task `POST`. On the VPS `LOOP_E2E_MODEL=claude-opus-5` — fable-5 sits on the
reviewer and the Advisor, and those two are what burn the subscription limit.

## 3. Skills baked into the sandbox image

`deploy/sandbox-image/skills/` → `/opt/sandbox-skel/.claude/skills/`. Right now that holds
`writing-plans` and `writing-specs` (vendored adaptations of the superpowers skills: interactive dialogs
and the default dated file names that clash with the fixed `issue-N` paths have been cut out) plus
`playwright-cli`.

**Put them strictly into `/opt/sandbox-skel`, not `/home/sandbox`** — otherwise the skill is invisible at
runtime ([[decisions/0004-skills-into-sandbox-skel]]). Post-build check:
`docker run --rm --entrypoint sh loop-sandbox:latest -c 'ls /opt/sandbox-skel/.claude/skills'`.

The effect measured live (smoke test issue #21 → Run#29, 2026-08-04): the spec grew from 6–12 KB to
14 KB, the plan came out at 654 lines, 7 tasks with real code and a Risks section; the planner figured
out on its own that the page uses a `<select>` rather than buttons. The price — planning took ~30 min
instead of ~10.

## 4. The **target** repository's `CLAUDE.md` and `.claude/skills/`

The agent is started with `cmd.Dir` = the repo clone, so it sees the repo's own instructions — provided
they are **committed** (check with `git ls-tree origin/main -- .claude/skills/...`). Verified
experimentally as far back as PR#3: the agent followed the smoke repo's `CLAUDE.md` (commit prefix,
docstring format). This is a full-fledged steering channel: a given project's rules live in that
project's own repository, not in the orchestrator.

The `Agent`, `Workflow` and `Skill` tools are available in the sandbox (probe 2026-08-04) — so the prompt
line "use the parallel-plan-execution skill if it is available" really does fire.

## 5. A strict JSON verdict in the final message

The reviewer, e2e, planner and advisor must all end with a JSON object of a fixed schema
(`review.VERDICT_SCHEMA`, `planning.PLANNER_OUTPUT_SCHEMA`, `ADVISOR_VERDICT_SCHEMA`).

**A gotcha that cost us a failed Run:** the greedy `\{.*\}` in the parsers broke whenever the agent wrote
something like `` `{op: ...}` `` in prose before the JSON. All four parsers now go through
`jsonextract.find_json_object` — `raw_decode` from every `{`, the last valid object wins, with priority
given to an object carrying the expected key (`9dc7727`).

The final message must be read from **both** fields: `agent_message_final` (the single GET) and
`agent_message` (the list) — [[concepts/sandboxd-platform]].

## What the agent must not do

- **The planner is forbidden to touch lockfiles.** It regenerated `uv.lock` without the dev group, ruff
  "unpinned itself", and a red PR rode into main. The prompt now allows committing only the spec and the
  plan; everything else gets `git checkout --`
  ([[decisions/0006-merge-gate-and-conflict-resolver]]).
- **Do not touch the repo-local git config** that blocks push (`f04c856`,
  `SandboxdClient.sanitize_git_config`).

## Links

- [[components/pipeline]] · [[concepts/sandboxd-platform]] · [[ops/sandbox-image]] · [[ops/target-repos]]
