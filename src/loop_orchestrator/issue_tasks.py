"""Backlog mirror: one row per loop:ready GitHub issue.

GitHub is the source of truth; the scheduler rebuilds these rows on every
tick, so any conflict resolves in GitHub's favour by construction.
"""
import json
from dataclasses import dataclass, field

import aiosqlite

BACKLOG = "backlog"
RUNNING = "running"
DONE = "done"
NEEDS_INFO = "needs_info"
FAILED = "failed"
WITHDRAWN = "withdrawn"


@dataclass
class IssueTask:
    id: int
    repo: str
    issue_number: int
    title: str
    lane: str | None
    state: str
    blocked_by: list[int]
    run_id: int | None
    topic_id: int | None
    updated_at: str
    # Every dependency ever seen, open or closed — see set_depends_on. Last and
    # defaulted so callers that predate the handoff keep constructing the task.
    depends_on: list[dict] = field(default_factory=list)


def _to_task(row: aiosqlite.Row) -> IssueTask:
    return IssueTask(
        id=row["id"], repo=row["repo"], issue_number=row["issue_number"],
        title=row["title"], lane=row["lane"], state=row["state"],
        blocked_by=json.loads(row["blocked_by"]),
        depends_on=json.loads(row["depends_on"]), run_id=row["run_id"],
        topic_id=row["topic_id"], updated_at=row["updated_at"])


async def upsert_task(db: aiosqlite.Connection, repo: str, issue_number: int,
                      title: str, lane: str | None) -> IssueTask:
    # updated_at anchors the needs_info comment poll (scheduler passes it as
    # GitHub's `since`), so a no-op sync must leave it untouched — otherwise
    # every tick moves the anchor forward and author answers are never seen.
    await db.execute(
        "INSERT INTO issue_tasks (repo, issue_number, title, lane) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(repo, issue_number) DO UPDATE SET title=excluded.title, "
        "lane=excluded.lane, updated_at=CASE WHEN issue_tasks.title IS NOT "
        "excluded.title OR issue_tasks.lane IS NOT excluded.lane "
        "THEN datetime('now') ELSE issue_tasks.updated_at END",
        (repo, issue_number, title, lane))
    await db.commit()
    return await get_task(db, repo, issue_number)


async def get_task(db: aiosqlite.Connection, repo: str, issue_number: int) -> IssueTask | None:
    async with db.execute(
            "SELECT * FROM issue_tasks WHERE repo=? AND issue_number=?",
            (repo, issue_number)) as cur:
        row = await cur.fetchone()
    return _to_task(row) if row else None


async def tasks_for_repo(db: aiosqlite.Connection, repo: str) -> list[IssueTask]:
    async with db.execute(
            "SELECT * FROM issue_tasks WHERE repo=? ORDER BY issue_number",
            (repo,)) as cur:
        rows = await cur.fetchall()
    return [_to_task(r) for r in rows]


async def _set(db: aiosqlite.Connection, repo: str, issue_number: int,
               column: str, value) -> None:
    await db.execute(
        f"UPDATE issue_tasks SET {column}=?, updated_at=datetime('now') "
        "WHERE repo=? AND issue_number=?",
        (value, repo, issue_number))
    await db.commit()


async def set_state(db, repo, issue_number, state: str) -> None:
    await _set(db, repo, issue_number, "state", state)


async def set_blocked_by(db, repo, issue_number, blockers: list[int]) -> None:
    await _set(db, repo, issue_number, "blocked_by", json.dumps(sorted(blockers)))


async def set_depends_on(db, repo, issue_number, deps: list[dict]) -> None:
    """Every dependency, open or closed — `blocked_by` forgets them on closing,
    and closing is exactly when the handoff needs them."""
    await _set(db, repo, issue_number, "depends_on", json.dumps(deps))


async def set_run(db, repo, issue_number, run_id: int) -> None:
    await _set(db, repo, issue_number, "run_id", run_id)


async def set_topic(db, repo, issue_number, topic_id: int | None) -> None:
    await _set(db, repo, issue_number, "topic_id", topic_id)


async def repos_with_tasks(db: aiosqlite.Connection) -> list[str]:
    async with db.execute("SELECT DISTINCT repo FROM issue_tasks ORDER BY repo") as cur:
        rows = await cur.fetchall()
    return [r["repo"] for r in rows]
