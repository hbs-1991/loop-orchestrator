# 0011 — No environment specifics in the repository

- **Status:** accepted
- **Date:** 2026-08-06
- **Related:** [[decisions/0010-documentation-in-english]] · [[ops/vps]] · [[ops/target-repos]] · `CLAUDE.md` §Conventions

## Context

The documentation was written for an audience of one, so it named the deployment directly: the VPS
address, the webhook and preview domain, the GitHub owner and org, the smoke and production
repository names, the sibling project sharing the host, and an absolute path on the author's Windows
machine. That was useful while the reader owned all of it.

For an open-source repository the same lines are a liability rather than a service. Nothing there is
a credential — no tokens, keys or chat ids ever reached the documents — but an IP address, a domain
and a list of private business repositories are an inventory of one person's infrastructure, published
permanently and indexed. They are also **wrong** for every reader but one, which quietly teaches
newcomers to copy values that cannot work for them.

## Decision

Documents describe the **shape** of a deployment, never one instance of it. Environment-specific
values are replaced by placeholders and live only where they are actually needed — `~/loop/.env` and
`~/.sandboxd/src/.env` on the host, plus GitHub repository secrets:

| Placeholder | Stands for |
|---|---|
| `<vps-ip>` | the host address |
| `loop.example.com` | the webhook and preview domain |
| `<owner>` / `<org>` | the GitHub account and organisation |
| `<backend-repo>` / `<frontend-repo>` / `<admin-repo>` | the production repositories under loop's control |
| `<sibling-project>` | the owner's other service sharing the VPS |

`.env.example` stays the one file that shows the **format** of every value — with example values only.

Ops pages carry a short note explaining the placeholders, so a reader is never left guessing whether
`<vps-ip>` is a literal.

## Alternatives

- *Keep the real values and sanitise at publication time* — rejected: a one-off scrub before a public
  push is exactly the step that gets forgotten, and git history keeps whatever slipped through.
- *Keep a private ops appendix inside the repo* — rejected: a private file in a public repository is a
  contradiction; the host's own `.env` already is that appendix.
- *Drop the ops pages entirely* — rejected: the operational knowledge (the idle reaper, the cron prune
  incident, the two-core limit) is some of the most valuable in the wiki. It is the identifiers that
  had to go, not the lessons.

## Consequences

- **New rule when writing docs:** if a value would differ for another reader, it is a placeholder.
  This applies to hosts, domains, accounts, repository names, ids and absolute local paths.
- The wiki's own upkeep rules ([[conventions]] §1) now carry this alongside the no-duplication rule.
- Cross-checking the docs against a live host now requires the host's `.env` — an accepted cost.
- Still owed before the repository actually goes public: a pass over the **git history**, which holds
  the pre-sanitisation versions of every one of these files.
