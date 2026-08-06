# Concept: delivering project secrets into the sandbox

## Why not through the app config

The obvious route — `POST /v1/apps/{id}/config` with `sensitive: true` — **does not work**: the config
never reaches the agent at all. Verified by a probe on 2026-08-05 (a throwaway app plus a task printing
whether it sees the variables) and confirmed against the sandboxd sources:

- `control-plane/internal/api/v1_app_config.go` — values are "owned by the control plane (not Docker
  env, workspace files, or task logs)", while the broker described by `access_policy`
  (`agent_access`/`runtime_access`) is **absent** from the code — those two comment lines are its only
  mention;
- `control-plane/cmd/runtimed/agentenv.go:85` strips from the agent's environment everything ending in
  `_KEY`/`_TOKEN`/`_SECRET`/`_PASSWORD`/`_CREDENTIALS`/`_APIKEY` or starting with `RUNTIMED_`.

The early phase-5 smoke test "`GH_TOKEN` reached the agent" was a **false positive**: the
`loop-smoke-test` repository is public, so an anonymous clone worked without a token too.

## How it is done

Secrets travel **as a file** (`src/loop_orchestrator/secrets.py`, `Pipeline._write_secrets`):

1. on the server there is a per-repo file `secrets/<owner>__<repo>.env` (directory `LOOP_SECRETS_DIR`,
   never committed to git);
2. its contents are written into the sandbox via
   `PUT /v1/sandboxes/{id}/files?path=.loop/secrets.env`, with values passed through `shlex.quote`;
3. alongside it goes a `.loop/.gitignore` containing `*` — in some repositories `.loop/` is committed,
   and `git add -A` could otherwise carry a secret into a commit;
4. the prompts of every stage name **only the key names** and the line
   `set -a; . .loop/secrets.env; set +a`.

Values end up neither in the prompt, nor in the Run record, nor in the logs. A failure to write the file
is a **fatal** stage error: working with half the secrets is worse than not starting.

Child processes spawned by the agent (git, Playwright, the dev server) inherit the values — the
`agentenv.go` filter only applies to spawning the agent itself. Verified by a live probe with the name
`GIT_SYNC_TOKEN`: `token_visible: true`, and a password containing a space, a quote and a `$` arrived
intact.

Uploading to the app config is still in the code (`SandboxdClient.set_app_secret`) — it is harmless and
will come in handy if the broker ever shows up.

## What kinds of secrets exist

- **Project secrets** — whatever `required_env` in the target repo's `.loop.yml` asks for (for example
  `GH_TOKEN` for a stack that clones a neighbouring repository).
- **The temporary `GIT_SYNC_TOKEN`** — issued only to the conflict resolver agent, for the sake of a
  correct git merge (the user's decision). The clone inside the sandbox has no credentials, so `fetch`
  is impossible otherwise.

## Links

- [[decisions/0002-secrets-as-file]] · [[concepts/sandboxd-platform]] · [[concepts/publication]]
- [[components/pipeline]] · [[components/storage-and-config]] · [[ops/target-repos]]
