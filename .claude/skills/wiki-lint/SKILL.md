---
name: wiki-lint
description: 'Checks the health of the project LLM-wiki (docs/wiki/): contradictions, stale claims, orphan pages, broken links, duplication of specs. Use on the user request or periodically as the wiki grows.'
---

# Wiki Lint

**Goal:** keep the wiki healthy as it grows. The rules are in `docs/wiki/conventions.md` §4 (Lint).

**When to run:** on the user request · after several ingests · before a major milestone
(closing a phase, moving infrastructure).

## EXECUTION

### Step 1: build the map
- Read `docs/wiki/index.md`, `overview.md`, `log.md` and list every page
  (`concepts/*`, `components/*`, `ops/*`, `decisions/*`).

### Step 2: checks
- **Contradictions** between pages (the same thing stated differently).
- **Stale claims** — the code, the VPS or the target repositories have moved on. Pay special attention
  to claims about sandboxd behaviour: it is someone else's service, it is updated without our knowledge,
  and `concepts/sandboxd-platform.md` is the most perishable page. Verify against the code and the
  platform sources, not from memory.
- **Orphans** — pages with no inbound links and not mentioned in `index.md`.
- **Broken `[[links]]`** and broken relative links to repository files (including line numbers that
  have drifted).
- **Duplication** of specs/plans/`CLAUDE.md` instead of a link (a §1 violation).
- **index/log divergence** — created pages with no line in `index.md`; significant events with no
  entry in `log.md`.
- **Commit links** — SHAs that are not in the history.

### Step 3: report
- A grouped list of findings with paths and proposed fixes.
- Suggest: which concepts are worth creating, which pages to split or merge.

### Step 4: fixes (once confirmed)
- With the user's consent apply the fixes (following the `/wiki-ingest` logic) and append to `docs/wiki/log.md`:
  `## [YYYY-MM-DD] lint | <outcome>`.

## STOP CONDITIONS
- HALT if `docs/wiki/` is missing.
- Do not "fix" silently — report first, fixes after confirmation (except plainly broken links).

## VERIFICATION
- Do not propose dragging into the wiki what belongs in a spec or a plan.
- Cross-check against the current code in `src/loop_orchestrator/` and against `CLAUDE.md` as sources of truth.
- Take the date from the session context.
