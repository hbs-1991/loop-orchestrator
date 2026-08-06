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
    "kind", "issue_number", "lane",
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
)


async def connect(path: str) -> aiosqlite.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    async with db.execute("PRAGMA table_info(runs)") as cur:
        have = {row["name"] for row in await cur.fetchall()}
    for col, decl in _MIGRATIONS:
        if col not in have:
            await db.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
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
         run.id),
    )
    await db.commit()


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
