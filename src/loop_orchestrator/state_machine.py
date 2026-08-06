import aiosqlite

from . import db as dbmod
from .models import (
    AWAITING_APPROVAL,
    CANCELLED,
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
    Run,
)

TRANSITIONS: dict[str, set[str]] = {
    QUEUED: {PREPARING, FAILED, CANCELLED},
    PREPARING: {EXECUTING, PLANNING, FAILED, CANCELLED},
    PLANNING: {PUBLISHING, REPORTING, FAILED, CANCELLED},
    EXECUTING: {REVIEWING, E2E_TESTING, STAGING, FAILED, CANCELLED},
    REVIEWING: {E2E_TESTING, STAGING, FAILED, CANCELLED},
    E2E_TESTING: {STAGING, FAILED, CANCELLED},
    STAGING: {AWAITING_APPROVAL, PUBLISHING, FAILED},
    AWAITING_APPROVAL: {PUBLISHING, EXECUTING, CANCELLED, FAILED},
    PUBLISHING: {REPORTING, FAILED},
    REPORTING: {DONE, FAILED},
}


class InvalidTransition(Exception):
    pass


async def transition(db: aiosqlite.Connection, run: Run, to_state: str, detail: str = "") -> None:
    if to_state not in TRANSITIONS.get(run.state, set()):
        raise InvalidTransition(f"{run.state} -> {to_state}")
    from_state = run.state
    run.state = to_state
    await dbmod.save_run(db, run)
    await dbmod.add_event(db, run.id, from_state, to_state, detail)
