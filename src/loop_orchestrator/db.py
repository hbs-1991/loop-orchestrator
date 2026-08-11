import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import ACTIVE_STATES, QUEUED, Run

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  head_branch TEXT NOT NULL,
  state TEXT NOT NULL,
  app_id TEXT, sandbox_id TEXT, task_id TEXT,
  spec_path TEXT, plan_path TEXT, prompt TEXT,
  timeout_minutes INTEGER NOT NULL DEFAULT 180,
  error TEXT, summary TEXT,
  test_cmd TEXT,
  review_enabled INTEGER NOT NULL DEFAULT 1,
  review_max_iterations INTEGER NOT NULL DEFAULT 2,
  review_iteration INTEGER NOT NULL DEFAULT 0,
  review_status TEXT,
  review_json TEXT,
  run_cmd TEXT,
  e2e_enabled INTEGER NOT NULL DEFAULT 0,
  e2e_max_iterations INTEGER NOT NULL DEFAULT 2,
  e2e_iteration INTEGER NOT NULL DEFAULT 0,
  e2e_status TEXT,
  e2e_json TEXT,
  e2e_env_json TEXT,
  pr_title TEXT,
  tg_thread_id INTEGER,
  tg_card_message_id INTEGER,
  approval_mode TEXT NOT NULL DEFAULT 'always',
  staging_branch TEXT,
  preview_url TEXT,
  sandbox_expires_at TEXT,
  merged_at TEXT,
  tg_approval_message_id INTEGER,
  kind TEXT NOT NULL DEFAULT 'pr',
  issue_number INTEGER,
  lane TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  from_state TEXT,
  to_state TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS issue_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  lane TEXT,
  state TEXT NOT NULL DEFAULT 'backlog',
  blocked_by TEXT NOT NULL DEFAULT '[]',
  run_id INTEGER,
  topic_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(repo, issue_number)
);
-- What a Run built, for the tasks its issue blocks. Keyed by the producing
-- issue: a consumer resolves it through issue_tasks.depends_on. A revise
-- re-runs the stage, so the row is replaced rather than appended to.
CREATE TABLE IF NOT EXISTS upstream_contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  run_id INTEGER,
  pr_number INTEGER,
  head_sha TEXT NOT NULL DEFAULT '',
  contract_md TEXT NOT NULL DEFAULT '',
  sources_json TEXT NOT NULL DEFAULT '[]',
  breaking_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(repo, issue_number)
);
-- Per-Run cost rollup. Jaeger holds the detail for LOOP_TRACE_RETENTION_DAYS;
-- these two tables outlive it and answer "what did we spend" without a query
-- language. A revise re-runs every stage, so both are keyed for replacement.
CREATE TABLE IF NOT EXISTS run_traces (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id),
  trace_id TEXT NOT NULL,
  api_calls INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  tokens_input INTEGER NOT NULL DEFAULT 0,
  tokens_cache_write INTEGER NOT NULL DEFAULT 0,
  tokens_cache_read INTEGER NOT NULL DEFAULT 0,
  tokens_output INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS run_stage_costs (
  run_id INTEGER NOT NULL REFERENCES runs(id),
  stage TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  fresh INTEGER,
  api_calls INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  tokens_input INTEGER NOT NULL DEFAULT 0,
  tokens_cache_write INTEGER NOT NULL DEFAULT 0,
  tokens_cache_read INTEGER NOT NULL DEFAULT 0,
  tokens_output INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (run_id, stage)
);
"""

_RUN_FIELDS = (
    "id", "repo", "pr_number", "head_branch", "state", "app_id", "sandbox_id",
    "task_id", "spec_path", "plan_path", "prompt", "timeout_minutes", "error", "summary",
    "test_cmd", "review_enabled", "review_max_iterations", "review_iteration",
    "review_status", "review_json",
    "run_cmd", "e2e_enabled", "e2e_max_iterations", "e2e_iteration",
    "e2e_status", "e2e_json", "e2e_env_json",
    "pr_title", "tg_thread_id", "tg_card_message_id",
    "approval_mode", "staging_branch", "preview_url", "sandbox_expires_at",
    "merged_at", "tg_approval_message_id",
    "kind", "issue_number", "lane", "tg_merge_message_id",
    "contract_enabled", "contract_status", "contract_json",
    "planner_model", "advisor_enabled", "advisor_model", "plan_max_iterations",
)

# Columns added after phase 1; applied to live databases via ALTER TABLE.
_MIGRATIONS = (
    ("test_cmd", "TEXT"),
    ("review_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("review_max_iterations", "INTEGER NOT NULL DEFAULT 2"),
    ("review_iteration", "INTEGER NOT NULL DEFAULT 0"),
    ("review_status", "TEXT"),
    ("review_json", "TEXT"),
    ("run_cmd", "TEXT"),
    ("e2e_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("e2e_max_iterations", "INTEGER NOT NULL DEFAULT 2"),
    ("e2e_iteration", "INTEGER NOT NULL DEFAULT 0"),
    ("e2e_status", "TEXT"),
    ("e2e_json", "TEXT"),
    ("e2e_env_json", "TEXT"),
    ("pr_title", "TEXT"),
    ("tg_thread_id", "INTEGER"),
    ("tg_card_message_id", "INTEGER"),
    ("approval_mode", "TEXT NOT NULL DEFAULT 'always'"),
    ("staging_branch", "TEXT"),
    ("preview_url", "TEXT"),
    ("sandbox_expires_at", "TEXT"),
    ("merged_at", "TEXT"),
    ("tg_approval_message_id", "INTEGER"),
    ("kind", "TEXT NOT NULL DEFAULT 'pr'"),
    ("issue_number", "INTEGER"),
    ("lane", "TEXT"),
    ("tg_merge_message_id", "INTEGER"),
    ("contract_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("contract_status", "TEXT"),
    ("contract_json", "TEXT"),
    ("planner_model", "TEXT"),
    ("advisor_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("advisor_model", "TEXT"),
    ("plan_max_iterations", "INTEGER"),
)

# issue_tasks columns added after phase 5a.
_ISSUE_TASK_MIGRATIONS = (
    ("depends_on", "TEXT NOT NULL DEFAULT '[]'"),
)


async def _add_missing_columns(db: aiosqlite.Connection, table: str,
                               migrations: tuple) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        have = {row["name"] for row in await cur.fetchall()}
    for col, decl in migrations:
        if col not in have:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def connect(path: str) -> aiosqlite.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await _add_missing_columns(db, "runs", _MIGRATIONS)
    await _add_missing_columns(db, "issue_tasks", _ISSUE_TASK_MIGRATIONS)
    await db.commit()
    return db


def _to_run(row: aiosqlite.Row) -> Run:
    return Run(**{f: row[f] for f in _RUN_FIELDS})


async def create_run(db: aiosqlite.Connection, repo: str, pr_number: int,
                     head_branch: str, pr_title: str | None = None) -> Run:
    cur = await db.execute(
        "INSERT INTO runs (repo, pr_number, head_branch, state, pr_title) "
        "VALUES (?, ?, ?, ?, ?)",
        (repo, pr_number, head_branch, QUEUED, pr_title),
    )
    await db.commit()
    run = await get_run(db, cur.lastrowid)
    await add_event(db, run.id, None, QUEUED)
    return run


async def create_planning_run(db: aiosqlite.Connection, repo: str, issue_number: int,
                              head_branch: str, title: str | None, lane: str | None) -> Run:
    cur = await db.execute(
        "INSERT INTO runs (repo, pr_number, head_branch, state, pr_title, kind, "
        "issue_number, lane) VALUES (?, 0, ?, ?, ?, 'planning', ?, ?)",
        (repo, head_branch, QUEUED, title, issue_number, lane),
    )
    await db.commit()
    run = await get_run(db, cur.lastrowid)
    await add_event(db, run.id, None, QUEUED)
    return run


async def get_run(db: aiosqlite.Connection, run_id: int) -> Run | None:
    async with db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
    return _to_run(row) if row else None


async def active_run_for_pr(db: aiosqlite.Connection, repo: str, pr_number: int) -> Run | None:
    marks = ",".join("?" * len(ACTIVE_STATES))
    async with db.execute(
        f"SELECT * FROM runs WHERE repo = ? AND pr_number = ? AND kind = 'pr' "
        f"AND state IN ({marks}) LIMIT 1",
        (repo, pr_number, *ACTIVE_STATES),
    ) as cur:
        row = await cur.fetchone()
    return _to_run(row) if row else None


async def repaintable_run_for_pr(db: aiosqlite.Connection, repo: str,
                                 pr_number: int) -> Run | None:
    """The finished, unmerged run whose merge keyboard tracks this PR's checks.

    Newest first: a re-run of the same PR leaves the older run's buttons behind,
    and only the latest message is worth following.
    """
    async with db.execute(
        "SELECT * FROM runs WHERE repo = ? AND pr_number = ? AND state = 'done' "
        "AND merged_at IS NULL AND tg_merge_message_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (repo, pr_number),
    ) as cur:
        row = await cur.fetchone()
    return _to_run(row) if row else None


async def active_run_for_issue(db: aiosqlite.Connection, repo: str, issue_number: int) -> Run | None:
    marks = ",".join("?" * len(ACTIVE_STATES))
    async with db.execute(
        f"SELECT * FROM runs WHERE repo = ? AND issue_number = ? AND state IN ({marks}) LIMIT 1",
        (repo, issue_number, *ACTIVE_STATES),
    ) as cur:
        row = await cur.fetchone()
    return _to_run(row) if row else None


async def save_run(db: aiosqlite.Connection, run: Run) -> None:
    await db.execute(
        """UPDATE runs SET state=?, app_id=?, sandbox_id=?, task_id=?, spec_path=?,
           plan_path=?, prompt=?, timeout_minutes=?, error=?, summary=?,
           test_cmd=?, review_enabled=?, review_max_iterations=?, review_iteration=?,
           review_status=?, review_json=?,
           run_cmd=?, e2e_enabled=?, e2e_max_iterations=?, e2e_iteration=?,
           e2e_status=?, e2e_json=?, e2e_env_json=?,
           pr_title=?, tg_thread_id=?, tg_card_message_id=?,
           approval_mode=?, staging_branch=?, preview_url=?,
           sandbox_expires_at=?, merged_at=?, tg_approval_message_id=?,
           kind=?, issue_number=?, lane=?, pr_number=?,
           tg_merge_message_id=?,
           contract_enabled=?, contract_status=?, contract_json=?,
           planner_model=?, advisor_enabled=?, advisor_model=?, plan_max_iterations=?,
           updated_at=datetime('now') WHERE id=?""",
        (run.state, run.app_id, run.sandbox_id, run.task_id, run.spec_path,
         run.plan_path, run.prompt, run.timeout_minutes, run.error, run.summary,
         run.test_cmd, run.review_enabled, run.review_max_iterations,
         run.review_iteration, run.review_status, run.review_json,
         run.run_cmd, run.e2e_enabled, run.e2e_max_iterations, run.e2e_iteration,
         run.e2e_status, run.e2e_json, run.e2e_env_json,
         run.pr_title, run.tg_thread_id, run.tg_card_message_id,
         run.approval_mode, run.staging_branch, run.preview_url,
         run.sandbox_expires_at, run.merged_at, run.tg_approval_message_id,
         run.kind, run.issue_number, run.lane, run.pr_number,
         run.tg_merge_message_id,
         run.contract_enabled, run.contract_status, run.contract_json,
         run.planner_model, run.advisor_enabled, run.advisor_model,
         run.plan_max_iterations,
         run.id),
    )
    await db.commit()


async def latest_run_for_issue(db: aiosqlite.Connection, repo: str, issue_number: int,
                               kind: str) -> Run | None:
    """Newest run of `kind` raised for this issue, finished or not."""
    async with db.execute(
        "SELECT * FROM runs WHERE repo = ? AND issue_number = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (repo, issue_number, kind),
    ) as cur:
        row = await cur.fetchone()
    return _to_run(row) if row else None


async def runs_in_states(db: aiosqlite.Connection, states: set[str]) -> list[Run]:
    marks = ",".join("?" * len(states))
    async with db.execute(f"SELECT * FROM runs WHERE state IN ({marks}) ORDER BY id", (*states,)) as cur:
        rows = await cur.fetchall()
    return [_to_run(r) for r in rows]


async def previous_app_ids(db: aiosqlite.Connection, repo: str, pr_number: int, before_run_id: int) -> list[str]:
    async with db.execute(
        "SELECT DISTINCT app_id FROM runs WHERE repo=? AND pr_number=? AND id<? AND app_id IS NOT NULL",
        (repo, pr_number, before_run_id),
    ) as cur:
        rows = await cur.fetchall()
    return [r["app_id"] for r in rows]


async def previous_app_ids_for_issue(db: aiosqlite.Connection, repo: str,
                                     issue_number: int, before_run_id: int) -> list[str]:
    async with db.execute(
        "SELECT DISTINCT app_id FROM runs WHERE repo=? AND issue_number=? "
        "AND id<? AND app_id IS NOT NULL",
        (repo, issue_number, before_run_id),
    ) as cur:
        rows = await cur.fetchall()
    return [r["app_id"] for r in rows]


async def add_event(db: aiosqlite.Connection, run_id: int, from_state: str | None,
                    to_state: str, detail: str = "") -> None:
    await db.execute(
        "INSERT INTO run_events (run_id, from_state, to_state, detail) VALUES (?, ?, ?, ?)",
        (run_id, from_state, to_state, detail),
    )
    await db.commit()


async def events_for_run(db: aiosqlite.Connection, run_id: int) -> list[tuple[str, str]]:
    """(to_state, created_at UTC) in insertion order — feeds the progress card."""
    async with db.execute(
        "SELECT to_state, created_at FROM run_events WHERE run_id=? ORDER BY id",
        (run_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [(r["to_state"], r["created_at"]) for r in rows]


def utcnow() -> str:
    """UTC timestamp in the run_events format ('YYYY-MM-DD HH:MM:SS')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_TOKEN_COLS = ("tokens_input", "tokens_cache_write", "tokens_cache_read",
               "tokens_output")


async def save_stage_cost(db: aiosqlite.Connection, run_id: int, stage: str,
                          model: str, fresh: bool | None, api_calls: int,
                          tool_calls: int, tokens: dict, cost_usd: float) -> None:
    """One row per (run, stage), replaced on a re-run.

    A revise sends a Run back through execute/review/e2e, and the second pass is
    the one that shipped — so the later numbers replace the earlier ones instead
    of accumulating into a total nobody asked for.
    """
    await db.execute(
        f"INSERT INTO run_stage_costs (run_id, stage, model, fresh, api_calls, "
        f"tool_calls, {', '.join(_TOKEN_COLS)}, cost_usd, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id, stage) DO UPDATE SET "
        "model=excluded.model, fresh=excluded.fresh, api_calls=excluded.api_calls, "
        "tool_calls=excluded.tool_calls, "
        + ", ".join(f"{c}=excluded.{c}" for c in _TOKEN_COLS)
        + ", cost_usd=excluded.cost_usd, updated_at=excluded.updated_at",
        (run_id, stage, model or "", None if fresh is None else int(fresh),
         api_calls, tool_calls, tokens.get("input", 0), tokens.get("cache_write", 0),
         tokens.get("cache_read", 0), tokens.get("output", 0), cost_usd, utcnow()),
    )
    await db.commit()


async def refresh_run_trace(db: aiosqlite.Connection, run_id: int,
                            trace_id: str) -> None:
    """Recompute the Run total from its stage rows."""
    async with db.execute(
        f"SELECT COALESCE(SUM(api_calls),0) a, COALESCE(SUM(tool_calls),0) t, "
        + ", ".join(f"COALESCE(SUM({c}),0) {c}" for c in _TOKEN_COLS)
        + ", COALESCE(SUM(cost_usd),0) c FROM run_stage_costs WHERE run_id=?",
        (run_id,),
    ) as cur:
        row = await cur.fetchone()
    await db.execute(
        f"INSERT INTO run_traces (run_id, trace_id, api_calls, tool_calls, "
        f"{', '.join(_TOKEN_COLS)}, cost_usd, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id) DO UPDATE SET trace_id=excluded.trace_id, "
        "api_calls=excluded.api_calls, tool_calls=excluded.tool_calls, "
        + ", ".join(f"{c}=excluded.{c}" for c in _TOKEN_COLS)
        + ", cost_usd=excluded.cost_usd, updated_at=excluded.updated_at",
        (run_id, trace_id, row["a"], row["t"], row["tokens_input"],
         row["tokens_cache_write"], row["tokens_cache_read"], row["tokens_output"],
         row["c"], utcnow()),
    )
    await db.commit()


async def trace_rollup_for_run(db: aiosqlite.Connection, run_id: int) -> dict | None:
    async with db.execute("SELECT * FROM run_traces WHERE run_id=?", (run_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    async with db.execute(
        "SELECT * FROM run_stage_costs WHERE run_id=? ORDER BY stage", (run_id,),
    ) as cur:
        out["stages"] = [dict(r) for r in await cur.fetchall()]
    return out


async def run_by_approval_message(db: aiosqlite.Connection,
                                  message_id: int | None) -> Run | None:
    if message_id is None:
        return None
    async with db.execute(
        "SELECT * FROM runs WHERE tg_approval_message_id = ? LIMIT 1",
        (message_id,),
    ) as cur:
        row = await cur.fetchone()
    return _to_run(row) if row else None


async def save_contract(db: aiosqlite.Connection, repo: str, issue_number: int,
                        run_id: int | None, pr_number: int | None, head_sha: str,
                        contract_md: str, sources: list[str],
                        breaking: list[str]) -> None:
    """One row per producing issue, replaced on every re-capture."""
    await db.execute(
        "INSERT INTO upstream_contracts (repo, issue_number, run_id, pr_number, "
        "head_sha, contract_md, sources_json, breaking_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(repo, issue_number) DO UPDATE SET run_id=excluded.run_id, "
        "pr_number=excluded.pr_number, head_sha=excluded.head_sha, "
        "contract_md=excluded.contract_md, sources_json=excluded.sources_json, "
        "breaking_json=excluded.breaking_json, created_at=excluded.created_at",
        (repo, issue_number, run_id, pr_number, head_sha, contract_md,
         json.dumps(sources), json.dumps(breaking)))
    await db.commit()


async def get_contract(db: aiosqlite.Connection, repo: str,
                       issue_number: int) -> dict | None:
    async with db.execute(
            "SELECT * FROM upstream_contracts WHERE repo=? AND issue_number=?",
            (repo, issue_number)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None
