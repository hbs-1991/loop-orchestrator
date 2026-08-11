import pytest

from loop_orchestrator import db as dbmod
from loop_orchestrator.models import (
    AWAITING_APPROVAL,
    CANCELLED,
    CONTRACTING,
    DONE,
    E2E_TESTING,
    EXECUTING,
    FAILED,
    PLANNING,
    PREPARING,
    PUBLISHING,
    QUEUED,
    REPORTING,
    REVIEWING,
    STAGING,
)
from loop_orchestrator.state_machine import TRANSITIONS, InvalidTransition, transition


async def test_happy_path_and_event(tmp_path):
    db = await dbmod.connect(str(tmp_path / "t.db"))
    run = await dbmod.create_run(db, "o/r", 1, "b")
    await transition(db, run, PREPARING, detail="start")
    assert run.state == PREPARING
    assert (await dbmod.get_run(db, run.id)).state == PREPARING
    async with db.execute(
        "SELECT from_state, to_state, detail FROM run_events WHERE run_id=? ORDER BY id", (run.id,)
    ) as cur:
        events = [tuple(r) for r in await cur.fetchall()]
    assert events == [(None, QUEUED, ""), (QUEUED, PREPARING, "start")]


async def test_invalid_transition(tmp_path):
    db = await dbmod.connect(str(tmp_path / "t.db"))
    run = await dbmod.create_run(db, "o/r", 1, "b")
    with pytest.raises(InvalidTransition):
        await transition(db, run, DONE)
    assert run.state == QUEUED


async def test_any_active_to_failed(tmp_path):
    db = await dbmod.connect(str(tmp_path / "t.db"))
    run = await dbmod.create_run(db, "o/r", 1, "b")
    await transition(db, run, PREPARING)
    await transition(db, run, EXECUTING)
    await transition(db, run, FAILED, detail="boom")
    assert run.state == FAILED


async def test_executing_to_reviewing_to_staging(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.state = EXECUTING
    await dbmod.save_run(db, run)
    await transition(db, run, REVIEWING)
    assert run.state == REVIEWING
    await transition(db, run, STAGING)
    assert run.state == STAGING
    await transition(db, run, PUBLISHING)
    assert run.state == PUBLISHING


async def test_reviewing_to_failed(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.state = REVIEWING
    await dbmod.save_run(db, run)
    await transition(db, run, FAILED, detail="boom")
    assert run.state == FAILED


async def test_reviewing_cannot_jump_to_done(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.state = REVIEWING
    await dbmod.save_run(db, run)
    with pytest.raises(InvalidTransition):
        await transition(db, run, DONE)


async def test_executing_to_e2e_to_staging(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.state = EXECUTING
    await dbmod.save_run(db, run)
    await transition(db, run, E2E_TESTING)
    assert run.state == E2E_TESTING
    await transition(db, run, STAGING)
    assert run.state == STAGING


async def test_reviewing_to_e2e(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.state = REVIEWING
    await dbmod.save_run(db, run)
    await transition(db, run, E2E_TESTING)
    assert run.state == E2E_TESTING


async def test_e2e_to_failed(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.state = E2E_TESTING
    await dbmod.save_run(db, run)
    await transition(db, run, FAILED, detail="boom")
    assert run.state == FAILED


async def test_e2e_cannot_jump_to_done(db):
    run = await dbmod.create_run(db, "o/r", 1, "b")
    run.state = E2E_TESTING
    await dbmod.save_run(db, run)
    with pytest.raises(InvalidTransition):
        await transition(db, run, DONE)


async def test_planning_flow_transitions(db):
    run = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", None)
    await transition(db, run, PREPARING)
    await transition(db, run, PLANNING)
    await transition(db, run, PUBLISHING)
    await transition(db, run, REPORTING)
    await transition(db, run, DONE)


async def test_planning_to_reporting_directly(db):
    run = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", None)
    await transition(db, run, PREPARING)
    await transition(db, run, PLANNING)
    await transition(db, run, REPORTING)  # questions outcome skips publishing


def test_phase4a_transitions_present():
    assert STAGING in TRANSITIONS[E2E_TESTING]
    assert TRANSITIONS[STAGING] == {AWAITING_APPROVAL, PUBLISHING, "failed"}
    assert TRANSITIONS[AWAITING_APPROVAL] == {PUBLISHING, EXECUTING, CANCELLED, "failed"}
    # cancel is allowed from every pre-staging active state
    for state in ("queued", "preparing", EXECUTING, "reviewing", E2E_TESTING):
        assert CANCELLED in TRANSITIONS[state]
    # publishing/reporting still cannot be cancelled
    assert CANCELLED not in TRANSITIONS[PUBLISHING]


async def test_contracting_sits_between_verification_and_staging(db):
    run = await dbmod.create_run(db, "o/r", 5, "b")
    for state in (PREPARING, EXECUTING, CONTRACTING, STAGING):
        await transition(db, run, state)
    assert run.state == STAGING


async def test_every_verification_stage_can_reach_contracting(db):
    assert CONTRACTING in TRANSITIONS[EXECUTING]
    assert CONTRACTING in TRANSITIONS[REVIEWING]
    assert CONTRACTING in TRANSITIONS[E2E_TESTING]
    assert TRANSITIONS[CONTRACTING] == {STAGING, FAILED, CANCELLED}
