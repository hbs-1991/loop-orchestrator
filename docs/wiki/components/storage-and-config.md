# Component: storage, states, configuration

- **Files:** `src/loop_orchestrator/db.py`, `models.py`, `state_machine.py`, `issue_tasks.py`,
  `config.py`, `loopconfig.py`, `secrets.py`
- **Tests:** `tests/test_db.py`, `test_state_machine.py`, `test_config.py`, `test_loopconfig.py`,
  `test_secrets.py`
- **Related:** [[concepts/run-lifecycle]] · [[concepts/secrets-delivery]] ·
  [[concepts/contract-handoff]]

## SQLite

A single database (`LOOP_DB_PATH`, `/app/data/loop.db` in the container, volume `./data:/app/data`),
accessed through `aiosqlite`. The schema is the `SCHEMA` constant in `db.py`, its evolution is the
`_MIGRATIONS` list applied at startup. Migrations of the live database ran on every deploy of phases
2–5a; there is no separate tool for it, and none is needed.

Tables: `runs` (+ `run_events` — the progress card is drawn from them), `issue_tasks` (a mirror of
`loop:ready` issues, states `backlog/running/done/needs_info/failed/withdrawn`) and
`upstream_contracts` (what a Run built, for the tasks its issue blocks — one row per producing issue,
`(repo, issue_number)` UNIQUE, latest write wins, no history; `save_contract`/`get_contract`,
[[concepts/contract-handoff]]).

**Migrations are per table now.** `_add_missing_columns(db, table, migrations)` runs `_MIGRATIONS`
against `runs` and `_ISSUE_TASK_MIGRATIONS` against `issue_tasks` — the latter added `depends_on`
(`TEXT NOT NULL DEFAULT '[]'`), the former the Run's three contract columns: `contract_enabled`
(set at prepare, "this Run is tied to an issue"), `contract_status`
(`produced|none|skipped|failed`, NULL until the stage is reached) and `contract_json` (the captured
verdict, duplicated onto the Run exactly as `review_json`/`e2e_json` are, so Telegram can render the
contract before the issue comment exists).

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
`setup`, `test`, `run`, `required_env`, `approval`, `review`, `e2e`, `planning`, `base_branch`,
`timeout_minutes`, `max_fix_iterations`. It only ever grows, and every addition defaults to the
behaviour that preceded it — an existing file in a target repo must never need an edit.

It is read **from the PR head branch** — overrides can be put right into the branch. Two exceptions,
both read from the **default** branch because the decision is taken before the branch in question
exists: `base_branch` (`resolve_base_branch`) and `planning.enabled` (`planning_enabled`, consulted
by the scheduler before it bootstraps an issue branch). Both are fail-safe: a missing, unreachable or
unparseable config yields the previous behaviour — the default branch, and planning left on — and the
real parse error surfaces later at `preparing`.

The `planning:` section is the per-repository half of the planning knobs, each falling back to the
`LOOP_*` setting it overrides ([[concepts/agent-steering]] §2):

| Key | Falls back to | Effect |
|---|---|---|
| `planning.enabled` | — (on) | `false`: the scheduler never opens a planning Run; issues stay in the backlog |
| `planning.model` | `LOOP_PLANNER_MODEL` | the planner's model |
| `planning.advisor.enabled` | — (on) | `false`: the first plan is published with no review round |
| `planning.advisor.model` | `LOOP_ADVISOR_MODEL` | the Implementor Advisor's model |
| `planning.advisor.max_iterations` | `LOOP_PLAN_MAX_ITERATIONS` | how many rewrites before escalation |

All but the first are **snapshotted onto the Run** at `preparing` (`runs.planner_model`,
`advisor_enabled`, `advisor_model`, `plan_max_iterations`): a config edited mid-Run must not change
the rules that Run started under.

## Secrets

`secrets.py` renders `.loop/secrets.env`, `.loop/.gitignore` and the hint line for the prompt; the
files themselves live on the server at `secrets/<owner>__<repo>.env` (they never reach git). The
mechanics, and why not app config — [[concepts/secrets-delivery]].
