"""Fail-safe forum-topic manager: create/rename/close degrade, never raise."""
import httpx
import respx

from loop_orchestrator.clients.tg_topics import TOPIC_NAME_LIMIT, TopicManager

BASE = "https://api.telegram.org/bottok"


def make_manager() -> TopicManager:
    return TopicManager(httpx.AsyncClient(base_url=BASE), chat_id=42)


@respx.mock
async def test_create_returns_thread_id():
    route = respx.post(f"{BASE}/createForumTopic").respond(
        200, json={"ok": True, "result": {"message_thread_id": 777}})
    tm = make_manager()
    assert await tm.create("⏳ feat: x · #5") == 777
    body = route.calls[0].request.content.decode()
    assert "42" in body and "feat: x" in body


@respx.mock
async def test_create_failure_degrades_to_none():
    respx.post(f"{BASE}/createForumTopic").respond(400, json={"ok": False})
    tm = make_manager()
    assert await tm.create("⏳ feat: x · #5") is None


@respx.mock
async def test_create_truncates_long_names():
    route = respx.post(f"{BASE}/createForumTopic").respond(
        200, json={"ok": True, "result": {"message_thread_id": 1}})
    tm = make_manager()
    await tm.create("x" * 500)
    import json as jsonlib
    sent = jsonlib.loads(route.calls[0].request.content)
    assert len(sent["name"]) == TOPIC_NAME_LIMIT


@respx.mock
async def test_rename_and_close_swallow_errors():
    respx.post(f"{BASE}/editForumTopic").respond(400, json={"ok": False})
    respx.post(f"{BASE}/closeForumTopic").respond(500)
    tm = make_manager()
    await tm.rename(777, "✅ feat: x · #5")  # must not raise
    await tm.close(777)                       # must not raise


@respx.mock
async def test_rename_and_close_hit_api():
    r1 = respx.post(f"{BASE}/editForumTopic").respond(200, json={"ok": True, "result": True})
    r2 = respx.post(f"{BASE}/closeForumTopic").respond(200, json={"ok": True, "result": True})
    tm = make_manager()
    await tm.rename(777, "✅ done")
    await tm.close(777)
    assert r1.called and r2.called
