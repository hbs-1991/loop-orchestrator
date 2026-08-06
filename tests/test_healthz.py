import httpx
from loop_orchestrator.main import create_app

from tests.test_config import _settings


async def test_healthz():
    app = create_app(_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
