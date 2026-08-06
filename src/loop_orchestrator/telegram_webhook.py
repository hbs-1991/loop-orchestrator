"""Inbound Telegram updates: inline-button clicks and revise replies.

Always answers 200 fast (Telegram retries anything else); the action itself
runs in a background task and reports its outcome into the run's thread.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, FastAPI, Request, Response

from . import db as dbmod
from .actions import ActionError

router = APIRouter()
log = logging.getLogger(__name__)

ACTION_CODES = {"ap": "approve", "dc": "discard", "cn": "cancel",
                "rs": "restart", "mg": "merge", "md": "merge_deploy"}


@router.post("/webhooks/telegram")
async def telegram_webhook(request: Request) -> Response:
    settings = request.app.state.settings
    secret = settings.telegram_webhook_secret
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        return Response(status_code=401)
    try:
        update = json.loads(await request.body())
    except ValueError:
        return Response(status_code=200)
    if cq := update.get("callback_query"):
        await _handle_callback(request.app, cq)
    elif msg := update.get("message"):
        await _handle_message(request.app, msg)
    return Response(status_code=200)


async def _handle_callback(app: FastAPI, cq: dict) -> None:
    tg, settings = app.state.tg, app.state.settings
    callback_id = cq.get("id", "")
    user_id = (cq.get("from") or {}).get("id")
    code, _, run_id_s = (cq.get("data") or "").partition(":")
    if user_id not in settings.admin_ids():
        await tg.answer_callback(callback_id, "not authorized")
        return
    if code not in ACTION_CODES or not run_id_s.isdigit():
        await tg.answer_callback(callback_id, "unknown action")
        return
    await tg.answer_callback(callback_id, "working on it…")
    button_message_id = (cq.get("message") or {}).get("message_id")
    task = asyncio.create_task(_run_action(
        app, ACTION_CODES[code], int(run_id_s), user_id, button_message_id))
    _keep(app, task)


async def _handle_message(app: FastAPI, msg: dict) -> None:
    settings = app.state.settings
    user_id = (msg.get("from") or {}).get("id")
    text = (msg.get("text") or "").strip()
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    if user_id not in settings.admin_ids() or not text or reply_to is None:
        return  # not a revise reply — ignore silently
    run = await dbmod.run_by_approval_message(app.state.db, reply_to)
    if run is None:
        return
    task = asyncio.create_task(_run_revise(app, run.id, user_id, text))
    _keep(app, task)


def _keep(app: FastAPI, task: asyncio.Task) -> None:
    """Hold a strong reference so in-flight action tasks are not GC'd."""
    tasks = getattr(app.state, "tg_tasks", None)
    if tasks is None:
        tasks = app.state.tg_tasks = set()
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _report(app: FastAPI, run_id: int, text: str) -> None:
    run = await dbmod.get_run(app.state.db, run_id)
    try:
        await app.state.tg.send(text, thread_id=run.tg_thread_id if run else None)
    except Exception:  # noqa: BLE001 — reporting is best-effort
        log.warning("action result delivery failed for run=%s", run_id, exc_info=True)


async def _run_action(app: FastAPI, name: str, run_id: int, actor: int,
                      button_message_id: int | None) -> None:
    tg, actions = app.state.tg, app.state.actions
    try:
        result = await getattr(actions, name)(run_id, actor)
        if button_message_id is not None:
            await tg.clear_buttons(button_message_id)
    except ActionError as e:
        result = f"⚠️ {e}"
    except Exception as e:  # noqa: BLE001 — never die silently
        log.warning("action %s failed for run=%s", name, run_id, exc_info=True)
        result = f"⚠️ {name} failed: {e!r}"
    await _report(app, run_id, result)


async def _run_revise(app: FastAPI, run_id: int, actor: int, feedback: str) -> None:
    try:
        result = await app.state.actions.revise(run_id, actor, feedback)
    except ActionError as e:
        result = f"⚠️ {e}"
    except Exception as e:  # noqa: BLE001
        log.warning("revise failed for run=%s", run_id, exc_info=True)
        result = f"⚠️ revise failed: {e!r}"
    await _report(app, run_id, result)
