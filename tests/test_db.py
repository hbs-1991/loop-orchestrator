import aiosqlite

from loop_orchestrator import db as dbmod
from loop_orchestrator.models import ACTIVE_STATES, DONE, EXECUTING, QUEUED


async def make_db(tmp_path):
    return await dbmod.connect(str(tmp_path / "t.db"))


async def test_create_and_get(tmp_path):
    db = await make_db(tmp_path)
    run = await dbmod.create_run(db, "o/r", 5, "feat/x")
    assert run.id == 1 and run.state == QUEUED and run.repo == "o/r"
    got = await dbmod.get_run(db, run.id)
    assert got == run
    assert await dbmod.get_run(db, 999) is None


async def test_active_run_for_pr(tmp_path):
    db = await make_db(tmp_path)
    run = await dbmod.create_run(db, "o/r", 5, "b")
    assert (await dbmod.active_run_for_pr(db, "o/r", 5)).id == run.id
    run.state = DONE
    await dbmod.save_run(db, run)
    assert await dbmod.active_run_for_pr(db, "o/r", 5) is None


async def test_save_roundtrip_and_states(tmp_path):
    db = await make_db(tmp_path)
    run = await dbmod.create_run(db, "o/r", 5, "b")
    run.state, run.app_id, run.summary = EXECUTING, "app1", "did things"
    await dbmod.save_run(db, run)
    assert (await dbmod.get_run(db, run.id)).app_id == "app1"
    assert [r.id for r in await dbmod.runs_in_states(db, ACTIVE_STATES)] == [run.id]


async def test_previous_app_ids(tmp_path):
    db = await make_db(tmp_path)
    r1 = await dbmod.create_run(db, "o/r", 5, "b")
    r1.app_id = "a1"
    await dbmod.save_run(db, r1)
    r2 = await dbmod.create_run(db, "o/r", 5, "b")
    assert await dbmod.previous_app_ids(db, "o/r", 5, r2.id) == ["a1"]
    assert await dbmod.previous_app_ids(db, "o/r", 5, r1.id) == []


async def test_review_fields_roundtrip(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.test_cmd = "pytest -q"
    run.review_enabled = False
    run.review_max_iterations = 3
    run.review_iteration = 2
    run.review_status = "escalated"
    run.review_json = '{"summary": "s", "fixed": [], "remaining": []}'
    await dbmod.save_run(db, run)
    got = await dbmod.get_run(db, run.id)
    assert got.test_cmd == "pytest -q"
    assert not got.review_enabled
    assert got.review_max_iterations == 3
    assert got.review_iteration == 2
    assert got.review_status == "escalated"
    assert "remaining" in got.review_json


async def test_migration_adds_review_columns(tmp_path):
    # Simulate a phase 1 database that predates the review columns.
    legacy = await aiosqlite.connect(str(tmp_path / "legacy.db"))
    await legacy.executescript("""
        CREATE TABLE runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repo TEXT NOT NULL, pr_number INTEGER NOT NULL, head_branch TEXT NOT NULL,
          state TEXT NOT NULL, app_id TEXT, sandbox_id TEXT, task_id TEXT,
          spec_path TEXT, plan_path TEXT, prompt TEXT,
          timeout_minutes INTEGER NOT NULL DEFAULT 180, error TEXT, summary TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO runs (repo, pr_number, head_branch, state) VALUES ('o/r', 1, 'b', 'done');
    """)
    await legacy.commit()
    await legacy.close()

    conn = await dbmod.connect(str(tmp_path / "legacy.db"))
    run = await dbmod.get_run(conn, 1)
    assert run.review_enabled  # default 1
    assert run.review_iteration == 0
    assert run.review_status is None
    await conn.close()


async def test_e2e_fields_roundtrip(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.run_cmd = "npm run dev"
    run.e2e_enabled = True
    run.e2e_max_iterations = 1
    run.e2e_iteration = 1
    run.e2e_status = "escalated"
    run.e2e_json = '{"summary": "s", "tests": [], "main_video": null}'
    run.e2e_env_json = '{"VITE_API_URL": "http://localhost:8000"}'
    await dbmod.save_run(db, run)
    got = await dbmod.get_run(db, run.id)
    assert got.run_cmd == "npm run dev"
    assert got.e2e_enabled
    assert got.e2e_max_iterations == 1
    assert got.e2e_iteration == 1
    assert got.e2e_status == "escalated"
    assert got.e2e_json == '{"summary": "s", "tests": [], "main_video": null}'
    assert got.e2e_env_json == '{"VITE_API_URL": "http://localhost:8000"}'


async def test_e2e_migration_on_old_db(tmp_path):
    # A phase-2 database (no e2e columns) must be upgraded by connect().
    path = str(tmp_path / "old.db")
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, "
        "pr_number INTEGER NOT NULL, head_branch TEXT NOT NULL, state TEXT NOT NULL, "
        "app_id TEXT, sandbox_id TEXT, task_id TEXT, spec_path TEXT, plan_path TEXT, "
        "prompt TEXT, timeout_minutes INTEGER NOT NULL DEFAULT 180, error TEXT, "
        "summary TEXT, test_cmd TEXT, review_enabled INTEGER NOT NULL DEFAULT 1, "
        "review_max_iterations INTEGER NOT NULL DEFAULT 2, "
        "review_iteration INTEGER NOT NULL DEFAULT 0, review_status TEXT, review_json TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    await conn.execute(
        "INSERT INTO runs (repo, pr_number, head_branch, state) VALUES ('o/r', 1, 'b', 'done')")
    await conn.commit()
    await conn.close()
    db2 = await dbmod.connect(path)
    got = await dbmod.get_run(db2, 1)
    assert got.e2e_enabled is False or got.e2e_enabled == 0
    assert got.e2e_status is None
    assert got.pr_title is None
    assert got.tg_thread_id is None
    assert got.tg_card_message_id is None
    await db2.close()


async def test_tg_fields_roundtrip(db):
    run = await dbmod.create_run(db, "o/r", 1, "b", pr_title="feat: web playground")
    run.tg_thread_id = 777
    run.tg_card_message_id = 555
    await dbmod.save_run(db, run)
    got = await dbmod.get_run(db, run.id)
    assert got.pr_title == "feat: web playground"
    assert got.tg_thread_id == 777
    assert got.tg_card_message_id == 555


async def test_create_run_without_title(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    assert run.pr_title is None


async def test_events_for_run_ordered(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")  # writes the queued event
    await dbmod.add_event(db, run.id, "queued", "preparing")
    await dbmod.add_event(db, run.id, "preparing", "executing")
    events = await dbmod.events_for_run(db, run.id)
    assert [s for s, _ in events] == ["queued", "preparing", "executing"]
    assert all(t for _, t in events)


async def test_phase4a_columns_roundtrip(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.approval_mode = "never"
    run.staging_branch = "loop/run-1"
    run.preview_url = "https://s-x-3000.preview.example.com"
    run.sandbox_expires_at = "2026-08-03 12:00:00"
    run.merged_at = "2026-08-03 13:00:00"
    run.tg_approval_message_id = 42
    await dbmod.save_run(db, run)
    got = await dbmod.get_run(db, run.id)
    assert got.approval_mode == "never"
    assert got.staging_branch == "loop/run-1"
    assert got.preview_url == "https://s-x-3000.preview.example.com"
    assert got.sandbox_expires_at == "2026-08-03 12:00:00"
    assert got.merged_at == "2026-08-03 13:00:00"
    assert got.tg_approval_message_id == 42


async def test_run_by_approval_message(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.tg_approval_message_id = 77
    await dbmod.save_run(db, run)
    assert (await dbmod.run_by_approval_message(db, 77)).id == run.id
    assert await dbmod.run_by_approval_message(db, 78) is None
    assert await dbmod.run_by_approval_message(db, None) is None


async def test_create_planning_run_defaults(db):
    run = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "Fix login", "auth")
    assert run.kind == "planning"
    assert run.pr_number == 0
    assert run.issue_number == 7
    assert run.lane == "auth"
    assert run.pr_title == "Fix login"
    assert run.state == "queued"


async def test_save_run_persists_pr_number(db):
    # A planning run gains its PR only at publish time; save_run must keep it
    # (a restart between publishing and reporting would otherwise resume with
    # the pr_number=0 sentinel and take the questions branch).
    run = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", None)
    run.pr_number = 51
    await dbmod.save_run(db, run)
    assert (await dbmod.get_run(db, run.id)).pr_number == 51


async def test_active_run_for_pr_ignores_planning_runs(db):
    planning = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", None)
    planning.pr_number = 5
    await dbmod.save_run(db, planning)
    assert await dbmod.active_run_for_pr(db, "o/r", 5) is None
    pr_run = await dbmod.create_run(db, "o/r", 5, "loop/issue-7")
    assert (await dbmod.active_run_for_pr(db, "o/r", 5)).id == pr_run.id


async def test_active_run_for_issue(db):
    run = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", None)
    assert (await dbmod.active_run_for_issue(db, "o/r", 7)).id == run.id
    run.state = "failed"
    await dbmod.save_run(db, run)
    assert await dbmod.active_run_for_issue(db, "o/r", 7) is None


async def test_previous_app_ids_for_issue(db):
    old = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", None)
    old.app_id = "app-old"
    old.state = "failed"
    await dbmod.save_run(db, old)
    new = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", None)
    assert await dbmod.previous_app_ids_for_issue(db, "o/r", 7, new.id) == ["app-old"]


def test_utcnow_format():
    s = dbmod.utcnow()
    assert len(s) == 19 and s[4] == "-" and s[13] == ":"
