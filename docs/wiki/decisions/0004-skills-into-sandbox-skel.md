# 0004 — Skills go into `/opt/sandbox-skel`, not into `/home/sandbox`

- **Status:** accepted
- **Date:** 2026-08-04
- **Related:** [[concepts/agent-steering]] · [[ops/sandbox-image]] · commit `2da2ce8`

## Context

The `playwright-cli` skill added to the image in phase 3 lived in `/home/sandbox/.claude/skills/` — and
**never reached the agent once**. sandboxd bind-mounts a per-sandbox loopback workspace over
`/home/sandbox`, so anything the image writes into the home directory is invisible at runtime. The home
is seeded once from `/opt/sandbox-skel` by a one-shot container (`cp -aT /opt/sandbox-skel/. /target/`,
`control-plane/internal/loopback/loopback.go`).

Found by inspecting a live sandbox: `/home/sandbox/.claude` existed but held only Claude Code session
state. E2E still worked — because the prompt names the CLI directly and `@playwright/cli` is installed
globally; SKILL.md was not load-bearing there.

## Decision

Every skill baked into the image goes into `/opt/sandbox-skel/.claude/skills/`. Mandatory check after
building the image:
`docker run --rm --entrypoint sh loop-sandbox:latest -c 'ls /opt/sandbox-skel/.claude/skills'`.

## Alternatives

- *Copy the skills into the home at task start* — rejected: an extra step in every Run, easy to lose;
  the skel seed does the same thing natively.
- *Rely on the prompt alone* — rejected: the `writing-plans`/`writing-specs` skills visibly raised
  planning quality (Run#29 smoke test), and a prompt alone does not replace that.

## Consequences

- Every new image skill is verified with the command above — "put it in the Dockerfile" does not equal "it arrived".
- The second skill channel is `.claude/skills/` **of the target repository itself**; it works, but only
  if the skill is committed (`git ls-tree origin/main -- .claude/skills/...`).
