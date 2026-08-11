import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from . import db as dbmod
from .actions import Actions
from .clients.github import GitHubClient
from .clients.sandboxd import SandboxdClient
from .clients.telegram import TelegramNotifier
from .config import Settings
from .pipeline import Pipeline
from .scheduler import Scheduler
from .telegram_webhook import router as tg_router
from .webhook import router
from .worker import Worker


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = await dbmod.connect(resolved.db_path)
        gh = GitHubClient(resolved.github_token)
        sb = SandboxdClient(resolved.sandboxd_url, resolved.sandboxd_api_key)
        tg = TelegramNotifier(resolved.telegram_bot_token, resolved.telegram_chat_id,
                              tz=resolved.tz)
        pipeline = Pipeline(db=db, settings=resolved, gh=gh, sb=sb, tg=tg)
        worker = Worker(db=db, settings=resolved, pipeline=pipeline)
        actions = Actions(db=db, settings=resolved, gh=gh, sb=sb, tg=tg,
                          worker=worker, pipeline=pipeline)
        scheduler = Scheduler(db=db, settings=resolved, gh=gh, worker=worker)
        worker.scheduler = scheduler
        worker.actions = actions
        app.state.db, app.state.worker, app.state.tg = db, worker, tg
        app.state.actions = actions
        app.state.scheduler = scheduler
        await worker.start()
        await scheduler.start()
        if resolved.public_url and resolved.telegram_webhook_secret:
            # Idempotent; failures degrade to log — buttons then need a manual setWebhook.
            await tg.set_webhook(
                resolved.public_url.rstrip("/") + "/webhooks/telegram",
                resolved.telegram_webhook_secret)
        # Recovery reports orphaned runs to GitHub/Telegram with retries; run it
        # in the background so a slow third party can't delay webhook readiness.
        recover_task = asyncio.create_task(worker.recover())
        yield
        recover_task.cancel()
        with suppress(asyncio.CancelledError):
            await recover_task
        await scheduler.stop()
        await worker.stop()
        for client in (gh, sb, tg):
            await client.aclose()
        await db.close()

    app = FastAPI(title="loop-orchestrator", lifespan=lifespan)
    app.state.settings = resolved
    app.include_router(router)
    app.include_router(tg_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    return app
