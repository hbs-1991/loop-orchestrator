# Wiki Conventions — schema and upkeep rules

This is the **schema file** of the project's LLM wiki (Karpathy pattern, the idea — [[decisions/0001-llm-wiki-memory-system]]).
It describes how the wiki is built and how the agent maintains it. Read it before creating or editing
wiki pages.

> The wiki is the **second layer** between the raw sources (specs, plans, code, live infrastructure)
> and the agent. The agent **owns this layer entirely**: it creates pages, updates them as knowledge
> appears, keeps the cross-links and the consistency. The human sets direction and asks questions —
> the wiki is kept by the agent.

The wiki is written in English, like every other document in the repository — the project is headed
for open source, so English is the only documentation language ([[decisions/0010-documentation-in-english]]).
Russian survives only in the live conversation with the user, never in a file.

---

## 1. What the wiki IS and what it does NOT do

**The wiki holds the plan-vs-reality delta and the knowledge earned along the way** — the part that
is in no document:

- what has actually been built and how it is wired in the code (`components/`);
- cross-cutting mechanics and invariants (`concepts/`);
- decisions taken **during implementation** (`decisions/`);
- knowledge about the live infrastructure: VPS, sandboxd, the sandbox image, target repos (`ops/`);
- gotchas, findings, probe and smoke test results — especially anything that contradicts the
  platform documentation.

**The wiki never carries environment specifics.** Host addresses, domains, GitHub accounts, target
repository names, ids and absolute local paths are written as placeholders — `<vps-ip>`,
`loop.example.com`, `<org>`, `<backend-repo>` and friends. If a value would be different for another
reader, it is a placeholder ([[decisions/0011-no-environment-specifics-in-the-repo]]). Real values
live on the host, in `~/loop/.env`.

**The wiki does NOT duplicate** (the main risk is drift — breaking this rule makes the wiki harmful):

| Source of truth | Where it lives | How the wiki refers to it |
|---|---|---|
| Product decisions, Locked Decisions | `docs/superpowers/specs/*.md` | a page **links** to a spec section, does not retell it |
| Phase tasks and their checkboxes | `docs/superpowers/plans/*.md` | a link to the plan, statuses are not copied |
| Repo rules, commands, architecture summary | `CLAUDE.md` | a link; the wiki explains **why**, does not repeat **what** |
| Step-by-step platform installation | `docs/deploy.md` | `ops/` pages link to it and add operational knowledge |
| How the code is built | `src/loop_orchestrator/` | `components/` gives a map and the gotchas, does not retell functions |

Caught yourself copying a spec or a plan into the wiki — **stop and put a link instead**.

A separate rule about sandboxd: the platform is **someone else's code on the VPS** (`~/.sandboxd/src`),
and its behaviour has diverged from its documentation many times. Everything verified by a probe or
by reading its sources goes into `concepts/sandboxd-platform.md` with a date and the verification
method — this is the most valuable and the most perishable class of knowledge in the project.

---

## 2. Structure

```
docs/wiki/
├── index.md          # catalogue of all pages (the map) — read first
├── overview.md       # "where we are now": phase, current focus, what is unfinished
├── log.md            # chronological journal (append-only, newest on top)
├── conventions.md    # this file — schema and rules
├── decisions/        # ADR-lite: decisions taken during implementation
├── components/       # one page per code subsystem
├── concepts/         # cross-cutting mechanics and invariants
└── ops/              # live infrastructure: VPS, deploy, image, target repos
```

**Growth principle:** a page is born together with the knowledge, not ahead of it. Empty stubs go
stale faster than they get filled — we do not create them.

---

## 3. Page formats

**Links between wiki pages** — of the form `[[components/pipeline]]`, `[[concepts/run-lifecycle]]`,
`[[decisions/0002-secrets-as-file]]`. A link to a page that does not exist yet is fine — it marks
that the page is worth creating.

**Links to sources of truth** — ordinary relative md links to real repository files, for example
`../superpowers/specs/2026-07-31-loop-engineering-mvp-design.md`.

**Links to code** — `src/loop_orchestrator/pipeline.py:387` (a path with a line number is clickable).
A line number is a perishable thing: give it only where it genuinely helps, and always next to the
function name.

**A component page** (`components/<subsystem>.md`) follows `components/_template.md`:
purpose · files · key invariants · gotchas · links.

**An ADR** (`decisions/NNNN-*.md`) follows `decisions/_template.md`: context · decision ·
alternatives · consequences. Numbering is continuous (0001, 0002, …).

---

## 4. Operations

### Ingest — new knowledge appeared
Trigger: a phase/feature is implemented · a decision is taken outside the spec · a gotcha is found ·
a probe proved something · a smoke test passed (or failed) · the state of the infrastructure changed.

1. Update/create a `components/` page — if the change affects how the subsystem is built, its
   invariants or its gotchas.
2. Update/create `concepts/` — if a cross-cutting mechanic is touched (Run lifecycle, publication,
   secrets delivery, agent steering, resilience).
3. Write `decisions/NNNN-*.md` — if the decision was taken **outside the spec** (otherwise the
   decision lives in the spec and the wiki only links to it).
4. Update `ops/` — if something changed on the VPS, in the sandbox image, in CI/deploy or in the set
   of target repositories.
5. Update `overview.md` — the current phase and what is unfinished.
6. Add a row to `index.md` if the page is new.
7. Add an entry to `log.md` (on top).

One event usually touches 3–6 pages — that is normal.

### Query — answering a question about the project
1. `index.md` → the pages you need → deeper.
2. If the synthesised answer is useful (a comparison, an analysis of how things connect) — **save it
   as a page**, so that knowledge accumulates instead of dissolving into the chat.

### Lint — periodic health check (on the human's request)
Look for: contradictions between pages; stale claims (the code has moved on); orphans (no inbound
links); broken `[[links]]` and relative links; concepts mentioned but never created; duplication of
specs/plans instead of a link; divergence from the real state of the VPS.

---

## 5. Format of the `log.md` journal

Append-only, new entries **on top**. Every entry starts with the same prefix — the journal is parsed
by simple means (`Select-String '^## \['`):

```
## [YYYY-MM-DD] <type> | <short title>
<1-3 lines: what happened, which pages are affected, links>
```

`<type>` ∈ `bootstrap` · `ingest` · `decision` · `probe` · `smoke` · `incident` · `query` · `lint`.

`probe` — checking platform behaviour with a one-off probe; `smoke` — a live run of the loop;
`incident` — a breakage of the live system and its analysis.

Take the date from the session context (the `currentDate` field in the system reminder), do not
invent it.

---

## 6. How it works (the machinery)

- **Reading:** the `SessionStart` hook (`.claude/hooks/wiki-session-start.ps1`) mixes `index.md` +
  `overview.md` + the tail of `log.md` into the context of every session. There is no need to open
  them separately — they are already in context; drill down into specific pages.
- **Writing:** the `Stop` hook (`.claude/hooks/wiki-stop-reminder.ps1`) unobtrusively reminds you if
  `src/`, `tests/`, `deploy/`, `.github/` or `docs/superpowers/` changed while `docs/wiki/` did not.
  The hook **does not synthesise anything itself** — it only reminds; the agent updates the wiki.

**Executor skills** (they carry out the §4 procedures, they do not duplicate them):
- `/wiki-ingest` — Ingest. Run it on the Stop hook reminder or manually.
- `/wiki-lint` — health check.

A pointer to the wiki is in `CLAUDE.md` (the "Project memory" section).
