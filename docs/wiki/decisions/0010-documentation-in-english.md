# 0010 — Every document in the repository is written in English

- **Status:** accepted (extends and partly supersedes [[decisions/0008-english-everywhere]])
- **Date:** 2026-08-06
- **Related:** `CLAUDE.md` §Conventions · [[conventions]] · reviewer-phase2 spec, Locked Decision 9

## Context

[[decisions/0008-english-everywhere]] put every system-facing text into English but deliberately kept
a carve-out: the project's own documents — specs, plans, this wiki, `docs/deploy.md`, the skills, the
hook texts — stayed in Russian, because their only readers were the owner and the agent.

That assumption no longer holds: the project is headed for **open source**. A repository whose specs,
plans and architectural memory are written in a language its readers do not share is closed in
practice — a contributor can read the code but not the reasoning behind it, which is exactly the part
this project invested in. The carve-out also had a second cost inside the team of one: half of the
prose in the repo was English and half Russian, so every new file started with a language decision.

## Decision

**Every file in the repository is English.** Specs, plans, `docs/wiki/**`, `docs/deploy.md`, `CLAUDE.md`,
`.claude/skills/**`, the hook message texts, `.env.example`, code comments and docstrings. A new
document is written in English from its first line — never "Russian now, translate later".

Russian survives in exactly one place: the **live conversation with the user**. It is never committed.

The existing Russian corpus (50 files, ~15,100 lines: 6 specs, 6 plans, the whole wiki, `CLAUDE.md`,
`docs/deploy.md`, the skills and hook texts) was translated in a single pass on 2026-08-06 by a
16-stream agent workflow, each stream verified against a snapshot of the Russian original for
structural loss (heading order, code fences, checkbox states, table rows, links).

## Alternatives

- *Bilingual documents (English body, Russian notes)* — rejected: mixing two languages in one file was
  already declared a bug by 0008, and it doubles the upkeep of every edit.
- *Translate only at the moment of open-sourcing* — rejected: the corpus grows faster than it would
  ever be translated, and a "translate later" backlog is a backlog that is never done.
- *Keep the plans Russian as historical records* — rejected: the plans are the most useful artefact
  for a newcomer (they carry full task-level reasoning), which is precisely why they must be readable.

## Consequences

- The language rule in `CLAUDE.md` and the wiki's [[conventions]] now says "documentation included";
  Locked Decision 9 of the reviewer-phase2 spec carries the amendment.
- Translating the executed plans changed them from a strict historical record into a readable one:
  one Global Constraints line in the MVP plan ("Telegram messages and PR comments are Russian") was
  rewritten to English, because the same plan's code blocks had their message strings translated and
  the line would otherwise contradict them. Everything else in the plans is a translation, not a
  revision.
- Test fixtures deliberately containing Cyrillic (`tests/test_tg_format.py`, `tests/test_telegram.py`)
  were **kept**: they now serve as non-ASCII regression coverage for the Telegram formatters. This is
  the single sanctioned exception, and it is data, not documentation.
- Before publishing the repository, the remaining ops-facing specifics (VPS IP, domains, chat ids,
  smoke-repo names) still need a pass — English is necessary for open source, not sufficient.
