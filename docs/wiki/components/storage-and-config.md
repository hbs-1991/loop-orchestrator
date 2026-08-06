# Component: storage, states, configuration

- **Files:** `src/loop_orchestrator/db.py`, `models.py`, `state_machine.py`, `issue_tasks.py`,
  `config.py`, `loopconfig.py`, `secrets.py`
- **Tests:** `tests/test_db.py`, `test_state_machine.py`, `test_config.py`, `test_loopconfig.py`,
  `test_secrets.py`
- **Related:** [[concepts/run-lifecycle]] · [[concepts/secrets-delivery]]

## SQLite

A single database (`LOOP_DB_PATH`, `/app/data/loop.db` in the container, volume `./data:/app/data`),
accessed through `aiosqlite`. The schema is the `SCHEMA` constant in `db.py`, its evolution is the
`_MIGRATIONS` list applied at startup. Migrations of the live database ran on every deploy of phases
2–5a; there is no separate tool for it, and none is needed.

Tables: `runs` (+ `run_events` — the progress card is drawn from them) and `issue_tasks` (a mirror of
`loop:ready` issues, states `backlog/running/done/needs_info/failed/withdrawn`).

**Gotcha:** `save_run` writes an **explicit** list of SET fields. Add a field — add it there too.
That is how planning Runs lost `pr_number`: the sentinel 0 stayed in the database, and a restart
between `publishing` and `reporting` would have taken the Run down the "questions" branch
(`008db01`).

## States

`models.py` holds the state constants and `ACTIVE_STATES`/`CANCELABLE`; `state_machine.TRANSITIONS`
is the table of allowed transitions, and `transition()` validates and records the event. The state
scheme is a Locked Decision of the MVP spec — change it only by updating the spec.

## Orchestrator settings

`config.Settings` (pydantic-settings, prefix `LOOP_`, `.env`). It is the **only** configuration
channel — reading `os.environ` behind `Settings`' back is not allowed.

The values on the VPS that differ from the defaults and matter semantically:
`LOOP_MAX_CONCURRENT_RUNS=2` (host resources), `LOOP_E2E_MODEL=claude-opus-5` (fable-5 is taken by
the reviewer and the advisor), `LOOP_SANDBOXD_URL=http://sandboxd:9000` (the host port returns 400),
`LOOP_TZ=Asia/Almaty`, `LOOP_BACKLOG_REPOS` — the list of production repositories.

## The target repository's `.loop.yml`

`loopconfig.parse_loop_config` — the format is a Locked Decision of the MVP spec: `specs_dir`,
`setup`, `test`, `run`, `required_env`, `approval`, `review`, `e2e`, `base_branch`,
`timeout_minutes`, `max_fix_iterations`.

It is read **from the PR head branch** — overrides can be put right into the branch. The exception is
`base_branch`: it is read from the **default** branch (`resolve_base_branch`), because an override
cannot say where to look for itself; if the config is missing or broken, the default branch is
returned silently, and the real parse error surfaces later at `preparing`.

## Secrets

`secrets.py` renders `.loop/secrets.env`, `.loop/.gitignore` and the hint line for the prompt; the
files themselves live on the server at `secrets/<owner>__<repo>.env` (they never reach git). The
mechanics, and why not app config — [[concepts/secrets-delivery]].
