# 0001 — The LLM wiki as project memory

- **Status:** accepted
- **Date:** 2026-08-06
- **Related:** [[conventions]] · `.claude/settings.json` · `.claude/hooks/` · `CLAUDE.md`

## Context

The project went through five phases in a week, and most of its knowledge lives neither in the code
nor in the specs but in dissected incidents: sandboxd behaves unlike its documentation (app config
never reaches the agent, the idle reaper cuts a Run off at minute 35, skills have to go into
`/opt/sandbox-skel`), the VPS falls over on three concurrent runs, GitHub indexes labels with a lag.
All of that lived in the agent's personal memory (`~/.claude/.../memory/`) as one sprawling 60-paragraph
file: not versioned, not visible in PRs, tied to one machine, and harder to search with every paragraph.

Meanwhile the phase specs and plans remain the source of truth for product decisions — the new memory
must **not duplicate** them, or it becomes a second, diverging description of the same system.

## Decision

1. **Storage:** in-repo `docs/wiki/` — versioned, visible in PRs, travels with the code.
2. **Boundaries:** the wiki holds the plan-vs-reality delta, knowledge about the live platform and
   ops knowledge; the specs (`docs/superpowers/specs/`), the plans and `CLAUDE.md` it **cites by link**,
   never retells.
3. **Structure:** `index` / `overview` / `log` / `conventions` + `decisions/` `components/`
   `concepts/` `ops/`. The `ops/` section departs from the reference layout: half of this project's
   knowledge is operational (VPS, sandbox image, target repositories), and it needs its own place.
4. **Mechanics — hooks:** `SessionStart` mixes `index` + `overview` + the tail of `log` into every
   session; `Stop` reminds you to update the wiki if `src/`, `tests/`, `deploy/`, `.github/` or
   `docs/superpowers/` changed and `docs/wiki/` did not. Plus the executor skills `/wiki-ingest` and
   `/wiki-lint`.
5. **Personal memory stays a pointer** to the wiki, not a second description of the project.
6. **Language — Russian**, like the rest of the project documents; English stays with the code, the
   prompts and external-facing texts.
   *(Reversed the same week: the whole documentation set moved to English on 2026-08-06 ahead of
   open-sourcing — [[decisions/0010-documentation-in-english]].)*

## Alternatives

- *Keep everything in the agent's personal memory* — rejected: not versioned, not visible in PRs, one
  file for everything, tied to one machine.
- *Dump the knowledge into `CLAUDE.md`* — rejected: it is already 60 lines, and every finding would
  want its own section; `CLAUDE.md` must stay a digest of rules, not a journal.
- *Extend the phase specs* — rejected: a spec describes the intent as of its phase, the wiki describes
  what actually came out of it; mixing them kills both documents.
- *A blocking Stop hook* — rejected: a reminder, not a ban; the synthesis is the agent's job anyway.

## Consequences

- If you touch the code or the infrastructure, update the wiki in the same pass — otherwise the Stop
  hook will remind you.
- The wiki has no right to duplicate the specs, the plans or `CLAUDE.md` — only to link to them (risk #1).
- The hook scripts are PowerShell on Windows; changing platform will require adapting them.
- Platform knowledge is perishable: every claim about sandboxd must carry a date and a way to verify it.
