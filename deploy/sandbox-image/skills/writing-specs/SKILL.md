---
name: writing-specs
description: Use when turning a task description into a design specification, before writing an implementation plan
---

# Writing Specs

<!--
Derived from superpowers:brainstorming (its design-document standard and spec
self-review) plus its spec-document-reviewer-prompt checklist, adapted for the
loop-orchestrator planning sandbox. The upstream skill is an interactive dialogue
gated on user approval; there is no user here, so the dialogue is replaced by
repository investigation and an explicit escape hatch (return questions instead of
guessing). Keep that adaptation when updating this copy from upstream.
-->

## Overview

Turn the task description into a design specification that an implementation plan can be
written from without asking anyone anything.

You have no user to ask mid-flight. Everything the spec asserts must come from the task
description, its discussion thread, or the repository itself — never from invention. When a
decision is genuinely undecidable from those three sources and choosing wrong would waste the
whole implementation, stop and return questions instead of writing a spec on a guess.

**Save the spec to the exact path you were given.** Do not invent a filename.

## Process

**1. Explore the repository first.** Read the project's instruction files (CLAUDE.md, `.claude/rules/`,
READMEs of the modules involved), the existing code paths the task touches, the test layout, and
recent commits in that area. A spec written before this step describes a codebase that does not exist.

**2. Check the scope.** If the task describes several independent subsystems, do not refine the
details of one and ignore the rest — say so in the spec's Goal section and decompose the work into
ordered pieces, each of which produces working, testable software on its own.

**3. Weigh 2-3 approaches.** Do not write down the first idea. Compare realistic alternatives
against the constraints you found in the repository, pick one, and record the alternatives and why
they lost — that reasoning is what stops the implementer from silently re-litigating the decision.

**4. Design for isolation.** Break the work into units with one clear purpose each, communicating
through well-defined interfaces. For every unit you must be able to answer: what does it do, how is
it used, what does it depend on? If a consumer has to read a unit's internals to use it, the
boundary is wrong.

**5. Work with the existing code, not against it.** Follow the patterns already in the repository.
Include a targeted improvement when the code you are changing is genuinely in the way; do not
propose unrelated refactoring.

## Spec Structure

Scale each section to its complexity — a few sentences when it is straightforward, a few paragraphs
when it is nuanced. Cover, at minimum:

- **Goal** — what changes for the user or the system when this is done, in one or two sentences.
- **Context** — current behavior, the modules and files involved, the constraints discovered in the
  repository, and anything the task's discussion thread settled.
- **Approach** — the chosen design, plus the alternatives considered and why they were rejected.
- **Components and data flow** — the units, their interfaces, how the data moves between them.
- **Error handling** — what fails, how it surfaces, what the system does about it.
- **Testing strategy** — what proves this works, at which level, with the project's own test runner.
- **Acceptance criteria** — observable, checkable statements. Each one must be something the
  implementation plan can produce a test or a command for.
- **Out of scope** — what this deliberately does not do.

## No Placeholders

The spec is the input to a plan written by another agent. These are **spec failures**:

- "TBD", "TODO", "to be decided later", empty sections
- Requirements phrased as vibes ("handle errors properly", "make it fast")
- Two sections that contradict each other
- A requirement that can be read two different ways
- Unrequested features and speculative generality — YAGNI ruthlessly

## Self-Review

After writing the spec, re-read it with fresh eyes against this checklist:

| Category | What to look for |
|---|---|
| Completeness | TODOs, placeholders, "TBD", unfinished sections |
| Consistency | Internal contradictions, conflicting requirements |
| Clarity | Requirements ambiguous enough to cause someone to build the wrong thing |
| Scope | Focused enough for a single implementation plan |
| YAGNI | Unrequested features, over-engineering |
| Groundedness | Every claim about the codebase traceable to a file you actually read |

Only fix things that would cause real problems for the plan: a missing section, a contradiction, a
requirement open to two readings. Wording preferences and unevenly detailed sections are not
issues. Fix inline and move on — no second review pass.

## When To Ask Instead

Return questions rather than a spec when — and only when — the task cannot be responsibly designed:
the goal itself is unclear, two stated requirements conflict irreconcilably, or a decision that
shapes the whole implementation has no answer in the task, the thread, or the repository.

Do not ask about things you can decide: file layout, naming, test placement, or anything the
repository's existing patterns already answer. An unnecessary question stalls the task until a
human comes back to it.
