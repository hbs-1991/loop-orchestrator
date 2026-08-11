from loop_orchestrator import issue_tasks as it


async def test_upsert_creates_backlog_task(db):
    task = await it.upsert_task(db, "o/r", 7, "Fix login", "auth")
    assert (task.state, task.lane, task.blocked_by) == (it.BACKLOG, "auth", [])


async def test_upsert_updates_title_and_lane_but_not_state(db):
    await it.upsert_task(db, "o/r", 7, "Fix login", "auth")
    await it.set_state(db, "o/r", 7, it.RUNNING)
    task = await it.upsert_task(db, "o/r", 7, "Fix login v2", None)
    assert (task.title, task.lane, task.state) == ("Fix login v2", None, it.RUNNING)


async def test_blocked_by_roundtrip(db):
    await it.upsert_task(db, "o/r", 7, "T", None)
    await it.set_blocked_by(db, "o/r", 7, [3, 5])
    assert (await it.get_task(db, "o/r", 7)).blocked_by == [3, 5]


async def test_tasks_for_repo_ordered_fifo(db):
    await it.upsert_task(db, "o/r", 9, "B", None)
    await it.upsert_task(db, "o/r", 7, "A", None)
    await it.upsert_task(db, "other/r", 1, "X", None)
    assert [t.issue_number for t in await it.tasks_for_repo(db, "o/r")] == [7, 9]


async def test_upsert_without_changes_keeps_updated_at(db):
    # updated_at is the `since` anchor for the needs_info comment poll: a
    # scheduler tick that changes nothing must not move it forward.
    await it.upsert_task(db, "o/r", 7, "T", "auth")
    await db.execute("UPDATE issue_tasks SET updated_at='2020-01-01 00:00:00' "
                     "WHERE repo='o/r' AND issue_number=7")
    await db.commit()
    task = await it.upsert_task(db, "o/r", 7, "T", "auth")
    assert task.updated_at == "2020-01-01 00:00:00"
    task = await it.upsert_task(db, "o/r", 7, "T v2", "auth")
    assert task.updated_at != "2020-01-01 00:00:00"


async def test_run_topic_and_repos(db):
    await it.upsert_task(db, "o/r", 7, "T", None)
    await it.set_run(db, "o/r", 7, 42)
    await it.set_topic(db, "o/r", 7, 777)
    task = await it.get_task(db, "o/r", 7)
    assert (task.run_id, task.topic_id) == (42, 777)
    assert await it.repos_with_tasks(db) == ["o/r"]


async def test_depends_on_survives_the_blocker_closing(db):
    await it.upsert_task(db, "o/frontend", 13, "T", None)
    await it.set_blocked_by(db, "o/frontend", 13, [12])
    await it.set_depends_on(db, "o/frontend", 13,
                            [{"repo": "o/backend", "number": 12}])
    # The blocker closes: the gate clears, the link does not.
    await it.set_blocked_by(db, "o/frontend", 13, [])
    task = await it.get_task(db, "o/frontend", 13)
    assert task.blocked_by == []
    assert task.depends_on == [{"repo": "o/backend", "number": 12}]


async def test_depends_on_defaults_to_empty(db):
    await it.upsert_task(db, "o/frontend", 14, "T", None)
    assert (await it.get_task(db, "o/frontend", 14)).depends_on == []
