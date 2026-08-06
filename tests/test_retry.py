import httpx
import pytest

from loop_orchestrator.clients.retry import with_retries


async def test_retries_transport_error_then_succeeds(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep(monkeypatch))
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return "ok"

    assert await with_retries(fn) == "ok"
    assert calls["n"] == 3


async def test_gives_up_after_attempts(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep(monkeypatch))

    async def fn():
        raise httpx.ConnectError("boom")

    with pytest.raises(httpx.ConnectError):
        await with_retries(fn)


async def test_no_retry_on_4xx():
    calls = {"n": 0}
    req = httpx.Request("GET", "http://t")

    async def fn():
        calls["n"] += 1
        raise httpx.HTTPStatusError("nope", request=req, response=httpx.Response(404, request=req))

    with pytest.raises(httpx.HTTPStatusError):
        await with_retries(fn)
    assert calls["n"] == 1


def _instant_sleep(monkeypatch):
    async def sleep(_):
        pass
    return sleep
