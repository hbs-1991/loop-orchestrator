# 0002 — Project secrets reach the sandbox as a file, not through app config

- **Status:** accepted
- **Date:** 2026-08-05
- **Related:** [[concepts/secrets-delivery]] · [[concepts/sandboxd-platform]] · commit `8551f45`

## Context

The first production run of the conflict-resolver agent came back with an honest "`GIT_SYNC_TOKEN` is
empty". A probe showed that neither a `sensitive: true` variable nor an ordinary config variable ever
appears in the agent's environment. From the sandboxd sources: `v1_app_config.go` keeps the values on
the control plane, and the broker its `access_policy` refers to is absent from the code; on top of
that `cmd/runtimed/agentenv.go` strips everything ending in `_TOKEN`/`_KEY`/`_SECRET`/`_PASSWORD` and
the like.

The phase 5 smoke test "`GH_TOKEN` reached the agent" turned out to be a false positive: the smoke
repo is public, so the anonymous clone worked without a token.

## Decision

Secrets are delivered as the file `.loop/secrets.env` via `PUT /v1/sandboxes/{id}/files`, values
`shlex.quote`-d. Next to it goes `.loop/.gitignore` containing `*`. Stage prompts name **only the key
names** and the line `set -a; . .loop/secrets.env; set +a`. Values land neither in the prompt nor in
the Run record. A failed write is a fatal stage error. The upload into app config stays as a harmless
head start.

## Alternatives

- *Wait for the broker in sandboxd* — rejected: it is not in the code and there is no ETA.
- *Pass secrets directly in the prompt* — rejected: the value would settle in task logs and in the Run record.
- *Patch sandboxd* — rejected: someone else's service, updates would overwrite it.

## Consequences

- A new project secret = a `secrets/<owner>__<repo>.env` file on the server, nothing else.
- The agent's child processes (git, Playwright) do inherit the values — the filter only applies to
  spawning the agent itself (verified by probe).
- In repositories where `.loop/` is committed, the protection against `git add -A` rests on
  `.loop/.gitignore` — do not touch it.
