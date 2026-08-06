---
name: wiki-ingest
description: 'Integrates new knowledge into the project LLM-wiki (docs/wiki/). Use it when a feature is implemented, a decision is made, a gotcha is found, an incident is analysed, a smoke test or a probe has run, or the Stop hook reminded you that docs/wiki/ has fallen behind. Executes the Ingest procedure from docs/wiki/conventions.md §4.'
---

# Wiki Ingest

**Goal:** integrate new knowledge into the project wiki so that it accumulates instead of dissolving
into chat. The rules live in `docs/wiki/conventions.md` — this skill executes them, not duplicates them.

**When to run:** a subsystem changed · a decision was made outside the spec · a gotcha was found · a probe
proved something about sandboxd · an incident was analysed · a smoke test passed (or failed) · the state
of the VPS / image / target repositories changed · the Stop hook reminded you.

## EXECUTION

### Step 0: get oriented
- Read `docs/wiki/conventions.md` (especially §1 boundaries and §4 Ingest) and `docs/wiki/index.md`.
- Work out exactly what knowledge appeared and which pages it touches.
- Take the date from the session context (the `currentDate` field), do not invent it.

### Step 1: component
- The change alters how a subsystem is built, its invariants or its gotchas → update
  `docs/wiki/components/<subsystem>.md` (a new page comes from `components/_template.md`).
- Write down what is **not visible** from the code at first glance: invariants, a call order that
  matters, external API behaviour. Do not retell functions.

### Step 2: concept
- A cross-cutting mechanic is affected (Run lifecycle, publication, secret delivery, agent
  management, resilience, platform behaviour) → update/create `docs/wiki/concepts/<concept>.md`.
- For claims about sandboxd, state the **date and the method of verification** (probe / sources / a live run).

### Step 3: decision
- The decision was made **outside the spec** → `docs/wiki/decisions/NNNN-<slug>.md` from `_template.md`
  (continuous numbering) + a line in `decisions/README.md`.
- The decision is already in the spec → do not copy it, add a link.

### Step 4: ops
- Something changed on the VPS, in the sandbox image, in CI/deploy or in the set of target repositories →
  update the corresponding page under `docs/wiki/ops/`.

### Step 5: overview
- Update `docs/wiki/overview.md`: the current focus and the "What is known and still open" section. Do not
  copy plan checkboxes.

### Step 6: index
- A new page appeared → a line in `docs/wiki/index.md`.

### Step 7: log
- Add an entry at the **top** of `docs/wiki/log.md`:
  `## [YYYY-MM-DD] <type> | <title>` + 1–3 lines (what, which pages, links to commits).
  Type: `ingest` · `decision` · `probe` · `smoke` · `incident` · `lint` · `refactor`.

## STOP CONDITIONS
- HALT if `docs/wiki/` is missing (the wiki has not been set up) — tell the user.
- The knowledge is already in the spec/plan/`CLAUDE.md` → do not copy it into the wiki; add a link or skip.

## VERIFICATION
- Links between wiki pages are `[[relative/path]]`; links to sources of truth are plain md links.
- No duplication of specs and plans (§1 of conventions — the main risk).
- One event may touch 3–6 pages — that is normal.
- The `log.md` entry is mandatory: it records that the ingest was performed.
