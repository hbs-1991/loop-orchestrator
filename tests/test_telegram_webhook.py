import asyncio
import json

import httpx
from fastapi import FastAPI

from loop_orchestrator import db as dbmod
from loop_orchestrator.actions import ActionError
from loop_orchestrator.telegram_webhook import router

from tests.conftest import FakeTG

SECRET = "tgsec"


class FakeActions:
    def __init__(self):
        self.calls: list[tuple] = []
        self.error: ActionError | None = None

    async def approve(self, run_id, actor):
        if self.error:
            raise self.error
        self.calls.append(("approve", run_id, actor))
        return "✅ approved"

    async def revise(self, run_id, actor, feedback):
        self.calls.append(("revise", run_id, actor, feedback))
        return "✏️ sent"


class Settings:
    telegram_webhook_secret = SECRET

    def admin_ids(self):
        return {100}


async def make_app(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings()
    app.state.db = await dbmod.connect(str(tmp_path / "t.db"))
    app.state.tg = FakeTG()
    app.state.actions = FakeActions()
    return app


async def post(app, update, secret=SECRET):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post("/webhooks/telegram", content=json.dumps(update).encode(),
                            headers={"X-Telegram-Bot-Api-Secret-Token": secret})


async def drain(app):
    """Wait out the background action tasks the endpoint spawned."""
    for _ in range(10):
        tasks = list(getattr(app.state, "tg_tasks", ()) or ())
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


def cb(data, user_id=100, message_id=555):
    return {"callback_query": {"id": "cb1", "from": {"id": user_id},
                               "data": data, "message": {"message_id": message_id}}}


async def test_bad_secret_rejected(tmp_path):
    app = await make_app(tmp_path)
    r = await post(app, cb("ap:1"), secret="wrong")
    assert r.status_code == 401
    assert app.state.actions.calls == []


async def test_callback_dispatches_action_and_clears_buttons(tmp_path):
    app = await make_app(tmp_path)
    run = await dbmod.create_run(app.state.db, "o/r", 1, "b")
    r = await post(app, cb(f"ap:{run.id}"))
    assert r.status_code == 200
    await drain(app)
    assert ("approve", run.id, 100) in app.state.actions.calls
    assert any(s.startswith("cb:cb1") for s in app.state.tg.sent)   # answered
    assert "clear:555" in app.state.tg.sent                          # buttons removed
    assert any("✅ approved" in s for s in app.state.tg.sent)        # result in thread


async def test_unauthorized_click_answered_without_action(tmp_path):
    app = await make_app(tmp_path)
    r = await post(app, cb("ap:1", user_id=999))
    assert r.status_code == 200
    await drain(app)
    assert app.state.actions.calls == []
    assert any("not authorized" in s for s in app.state.tg.sent)


async def test_action_error_reported_to_thread(tmp_path):
    app = await make_app(tmp_path)
    app.state.actions.error = ActionError("run #1 is already done")
    await post(app, cb("ap:1"))
    await drain(app)
    assert any("already done" in s for s in app.state.tg.sent)
    assert "clear:555" not in app.state.tg.sent  # buttons stay on failure


async def test_reply_to_approval_message_triggers_revise(tmp_path):
    app = await make_app(tmp_path)
    run = await dbmod.create_run(app.state.db, "o/r", 1, "b")
    run.tg_approval_message_id = 321
    await dbmod.save_run(app.state.db, run)
    update = {"message": {"from": {"id": 100}, "text": "make it blue",
                          "reply_to_message": {"message_id": 321}}}
    await post(app, update)
    await drain(app)
    assert ("revise", run.id, 100, "make it blue") in app.state.actions.calls


async def test_stray_messages_ignored(tmp_path):
    app = await make_app(tmp_path)
    for update in (
        {"message": {"from": {"id": 100}, "text": "hello"}},                # no reply
        {"message": {"from": {"id": 999}, "text": "x",
                     "reply_to_message": {"message_id": 321}}},             # not admin
        {"message": {"from": {"id": 100}, "text": "x",
                     "reply_to_message": {"message_id": 999}}},             # unknown msg
        {"edited_message": {"text": "x"}},                                  # other update
    ):
        r = await post(app, update)
        assert r.status_code == 200
    await drain(app)
    assert app.state.actions.calls == []
