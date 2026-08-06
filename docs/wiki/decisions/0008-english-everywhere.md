# 0008 — English in every system-facing text

- **Status:** accepted
- **Date:** 2026-07-31
- **Related:** `CLAUDE.md` §Conventions · [[concepts/agent-steering]]

## Context

Phase 1 wrote prompts, PR comments and Telegram messages in Russian. That made the system
non-transferable: the agent in the sandbox gets a Russian prompt and an English repository, PR comments
are read by project participants, and labels and verdicts are part of a machine contract.

## Decision

The user's decision: **every system-facing text is in English** — agent prompts, verdict schemas,
PR comments, Telegram messages, label descriptions, code and comments. Only the project documents
(specs, plans, this wiki) and the conversation with the user stay in Russian.

Migrating the Russian texts of phase 1 was folded into the scope of phase 2 and is done.

## Alternatives

- *Keep Russian in Telegram and English in the code* — rejected: the boundary would run through the
  middle of `pipeline.py`, and every new message would require a decision about its language.

## Consequences

- Any new text seen by someone other than the user is written in English by default.
- The flip side: the documents (this wiki included) are in Russian; mixing the two inside one file
  counts as a bug.

## Superseded in part (2026-08-06)

The document carve-out above is gone: ahead of open-sourcing, **every** file in the repository is
English, and Russian survives only in the live conversation with the user
([[decisions/0010-documentation-in-english]]). The rest of this decision — English for all
system-facing texts — still stands, and 0010 is its extension rather than its reversal.
