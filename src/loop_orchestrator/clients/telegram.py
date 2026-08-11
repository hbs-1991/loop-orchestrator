import html
import json
import logging

import httpx

from ..models import CANCELABLE, Run
from .retry import with_retries
from .tg_card import _ref, render_card, run_title, topic_final_name, topic_name
from .tg_format import md_to_telegram_html
from .tg_topics import TopicManager

log = logging.getLogger(__name__)


def _with_thread(payload: dict, thread_id: int | None) -> dict:
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    return payload


def approve_kb(run_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"ap:{run_id}"},
        {"text": "❌ Discard", "callback_data": f"dc:{run_id}"},
    ]]}


def cancel_kb(run_id: int) -> dict:
    return {"inline_keyboard": [[{"text": "⛔ Cancel", "callback_data": f"cn:{run_id}"}]]}


def merge_kb(run_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "🔀 Merge PR", "callback_data": f"mg:{run_id}"},
        {"text": "🚀 Merge & Deploy", "callback_data": f"md:{run_id}"},
    ]]}


def gate_kb(run_id: int, state: str, red: list[str] | None = None,
            done: int = 0, total: int = 0) -> dict:
    """The merge keyboard as an indicator of what the gate currently sees.

    Telegram has no disabled button, so a merge button that would only be
    refused is not shown at all — the row says what is missing instead. Since
    the target repos' CI moved to a two-slot self-hosted pool the wait is long
    enough that a blind press was the normal way to use these buttons.
    """
    if state == "clean":
        return merge_kb(run_id)
    if state == "conflicts":
        # Pressing this really does resolve and then merge, so `mg` is honest.
        return {"inline_keyboard": [[
            {"text": "🔧 Resolve & merge", "callback_data": f"mg:{run_id}"}]]}
    if state == "behind":
        return {"inline_keyboard": [[
            {"text": "⤴️ Update branch", "callback_data": f"ub:{run_id}"}]]}
    if state == "checks_failed":
        names = ", ".join(red or [])[:40] or "?"
        return {"inline_keyboard": [[
            {"text": f"🔴 CI red: {names}", "callback_data": f"ck:{run_id}"}]]}
    progress = f"{done}/{total}" if total else "running"
    return {"inline_keyboard": [[
        {"text": f"⏳ CI {progress}", "callback_data": f"ck:{run_id}"}]]}


def restart_kb(run_id: int) -> dict:
    return {"inline_keyboard": [[{"text": "🔁 Restart", "callback_data": f"rs:{run_id}"}]]}


class TelegramNotifier:
    def __init__(self, token: str, chat_id: int, tz: str = "UTC",
                 client: httpx.AsyncClient | None = None):
        self.chat_id = chat_id
        self.tz = tz
        self._owns_http = client is None
        self._http = client or httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}", timeout=30)
        self.topics = TopicManager(self._http, chat_id)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def send(self, text: str, thread_id: int | None = None,
                   reply_markup: dict | None = None) -> None:
        async def call() -> None:
            payload = _with_thread({
                "chat_id": self.chat_id, "text": text[:4000],
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, thread_id)
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            r = await self._http.post("/sendMessage", json=payload)
            r.raise_for_status()
        await with_retries(call)

    async def send_rich_markdown(self, markdown: str, fallback_html: str,
                                 thread_id: int | None = None,
                                 reply_markup: dict | None = None) -> int | None:
        """Bot API 10.x rich message: Telegram renders raw markdown natively
        (tables included) — no entity conversion on our side. Falls back to the
        parse_mode=HTML path when the API rejects it or does not support it.

        Returns the message_id when the API reported one — the caller keeps it
        to repaint the keyboard later. None is not an error: the fallback path
        and a malformed response both land there, and every caller treats a
        missing id as "this message simply will not be updated".
        """
        sent: dict = {}

        async def call() -> None:
            payload = _with_thread({
                "chat_id": self.chat_id,
                "rich_message": {"markdown": markdown[:4000]},
            }, thread_id)
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            r = await self._http.post("/sendRichMessage", json=payload)
            r.raise_for_status()
            body = r.json()
            if not body.get("ok", True):
                raise RuntimeError("sendRichMessage returned ok=false")
            sent["id"] = (body.get("result") or {}).get("message_id")
        try:
            await with_retries(call)
        except Exception:
            await self.send(fallback_html, thread_id=thread_id,
                            reply_markup=reply_markup)
            return None
        mid = sent.get("id")
        return int(mid) if isinstance(mid, int) else None

    async def send_video(self, video: bytes, filename: str, caption: str,
                         thread_id: int | None = None) -> None:
        """Upload a video (mp4 plays inline; anything else goes as a document).
        Delivery failures degrade to a text message — never fail the run."""
        is_mp4 = filename.endswith(".mp4")
        method = "/sendVideo" if is_mp4 else "/sendDocument"
        part = "video" if is_mp4 else "document"
        mime = "video/mp4" if is_mp4 else "application/octet-stream"
        data = {"chat_id": str(self.chat_id), "caption": caption[:1000]}
        if thread_id is not None:
            data["message_thread_id"] = str(thread_id)

        async def call() -> None:
            r = await self._http.post(method, data=data,
                                      files={part: (filename, video, mime)})
            r.raise_for_status()
        try:
            await with_retries(call)
        except Exception:
            await self.send(f"{caption}\n⚠️ video upload failed", thread_id=thread_id)

    # -- run thread lifecycle -------------------------------------------------

    async def start_run_thread(self, run: Run) -> int | None:
        return await self.topics.create(topic_name(run))

    async def finish_run_thread(self, run: Run) -> None:
        if run.tg_thread_id is None:
            return
        await self.topics.rename(run.tg_thread_id, topic_final_name(run))
        await self.topics.close(run.tg_thread_id)

    # -- progress card --------------------------------------------------------

    async def send_card(self, run: Run, events: list[tuple[str, str]]) -> int | None:
        """Silent checklist message; returns its message_id (None on failure)."""
        try:
            payload = _with_thread({
                "chat_id": self.chat_id,
                "text": render_card(run, events, self.tz)[:4000],
                "parse_mode": "HTML", "disable_web_page_preview": True,
                "disable_notification": True,
            }, run.tg_thread_id)
            if run.state in CANCELABLE:
                payload["reply_markup"] = cancel_kb(run.id)
            r = await self._http.post("/sendMessage", json=payload)
            r.raise_for_status()
            return int(r.json()["result"]["message_id"])
        except Exception:
            log.warning("progress card send failed for run=%s", run.id, exc_info=True)
            return None

    async def update_card(self, run: Run, events: list[tuple[str, str]]) -> None:
        if run.tg_card_message_id is None:
            return
        try:
            payload = {
                "chat_id": self.chat_id, "message_id": run.tg_card_message_id,
                "text": render_card(run, events, self.tz)[:4000],
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }
            # An edit without the key drops the button — exactly right once the
            # run leaves a cancelable state.
            if run.state in CANCELABLE:
                payload["reply_markup"] = cancel_kb(run.id)
            r = await self._http.post("/editMessageText", json=payload)
            if r.status_code == 400 and "not modified" in r.text:
                return
            r.raise_for_status()
        except Exception:
            log.warning("progress card edit failed for run=%s", run.id, exc_info=True)

    # -- final notifications --------------------------------------------------

    def _pr_url(self, run: Run) -> str:
        return _ref(run)[0]  # issue URL for planning runs (pr_number is 0 there)

    def _sub_md(self, run: Run) -> str:
        url, ref = _ref(run)
        return f"[{ref}]({url}) · Run {run.id}"

    def _sub_html(self, run: Run) -> str:
        url, ref = _ref(run)
        return f'<a href="{url}">{ref}</a> · Run {run.id}'

    def _status_lines(self, run: Run) -> str:
        review_line = ""
        if run.review_status == "clean":
            review_line = f"Review: ✅ clean ({run.review_iteration} fix iteration(s))\n"
        elif run.review_status == "escalated":
            review_line = "Review: ⚠️ findings remain — see the PR comment\n"
        elif run.review_status == "skipped":
            review_line = "Review: ⛔ skipped (see the PR note)\n"
        e2e_line = ""
        if run.e2e_status == "passed":
            e2e_line = f"E2E: ✅ passed ({run.e2e_iteration} fix iteration(s)) 🎬\n"
        elif run.e2e_status == "escalated":
            e2e_line = "E2E: ⚠️ failures remain — see the PR comment\n"
        elif run.e2e_status == "skipped":
            e2e_line = "E2E: ⛔ skipped (see the PR note)\n"
        contract_line = ""
        if run.contract_status == "produced":
            contract_line = "Contract: 📄 captured for the tasks this one blocks\n"
        elif run.contract_status == "none":
            contract_line = "Contract: 📄 no external interface changed\n"
        elif run.contract_status == "failed":
            contract_line = "Contract: ⚠️ not captured — dependent tasks will ask\n"
        return review_line + e2e_line + contract_line

    def _contract_block(self, run: Run) -> str:
        """The captured contract, for the one message a human answers.

        The issue comment does not exist yet at this point — it is published
        after approve — so rejecting a wrong contract is only possible if the
        text travels here.
        """
        if run.contract_status != "produced" or not run.contract_json:
            return ""
        try:
            data = json.loads(run.contract_json)
        except ValueError:
            return ""
        body = (data.get("contract") or "")[:1200]
        if not body:
            return ""
        return ("\n📄 <b>API contract for dependent tasks</b>"
                f"<blockquote expandable>{md_to_telegram_html(body)}</blockquote>")

    async def notify_done(self, run: Run) -> int | None:
        status_lines = self._status_lines(run)
        t = run_title(run)
        summary_md = (run.summary or "(no summary)")[:3200]
        markdown = (f"✅ **{t}** — finished\n{self._sub_md(run)}\n"
                    f"{status_lines}\n{summary_md}")
        head = (f"✅ <b>{html.escape(t)}</b> — finished\n{self._sub_html(run)}\n"
                f"{status_lines}")
        text = f"{head}<blockquote expandable>{md_to_telegram_html(summary_md)}</blockquote>"
        if len(text) > 4000:
            # Rich version would be cut mid-tag by Telegram's 4096 limit —
            # fall back to plain escaped text, which survives any truncation.
            text = f"{head}\n{html.escape(run.summary or '')[:3400]}"
        # The id goes into the run so the reaper can repaint these buttons as
        # the PR's checks move; the initial keyboard is deliberately the plain
        # merge pair, because at this moment the gate has not been read yet.
        return await self.send_rich_markdown(markdown, fallback_html=text,
                                             thread_id=run.tg_thread_id,
                                             reply_markup=merge_kb(run.id))

    async def notify_review_escalation(self, run: Run, remaining: int) -> None:
        t = run_title(run)
        body = (f"⚠️ %s: review is not clean after {run.review_iteration} "
                f"fix iteration(s), {remaining} finding(s) remain — "
                f"your attention is needed: {self._pr_url(run)}")
        await self.send_rich_markdown(
            body % f"**{t}**", fallback_html=body % f"<b>{html.escape(t)}</b>",
            thread_id=run.tg_thread_id)

    async def notify_e2e_escalation(self, run: Run, failed: int) -> None:
        t = run_title(run)
        body = (f"⚠️ %s: e2e is not green after {run.e2e_iteration} "
                f"fix iteration(s), {failed} failing scenario(s) — "
                f"your attention is needed: {self._pr_url(run)}")
        await self.send_rich_markdown(
            body % f"**{t}**", fallback_html=body % f"<b>{html.escape(t)}</b>",
            thread_id=run.tg_thread_id)

    async def notify_failed(self, run: Run) -> None:
        t = run_title(run)
        error = run.error or "unknown error"
        markdown = (f"❌ **{t}** — failed\n{self._sub_md(run)}\n"
                    f"```\n{error[:3000]}\n```")
        await self.send_rich_markdown(
            markdown,
            fallback_html=(f"❌ <b>{html.escape(t)}</b> — failed\n{self._sub_html(run)}\n"
                           f"<blockquote>{html.escape(error)[:3400]}</blockquote>"),
            thread_id=run.tg_thread_id, reply_markup=restart_kb(run.id))

    async def notify_awaiting_approval(self, run: Run) -> int | None:
        """Pushing approval request with buttons; returns its message_id."""
        t = run_title(run)
        # The paused sandbox is stopped, and the first hit on the link wakes it
        # (~10 s, and it can answer 502 while the server binds). Saying so beats
        # a reviewer reading that 502 as a broken build.
        preview_line = (f'🔗 <a href="{run.preview_url}">preview</a> '
                        "<i>(asleep — first open takes ~10 s, reload if it 502s)</i>\n"
                        if run.preview_url else "🔗 preview unavailable\n")
        head = (f"⏸ <b>{html.escape(t)}</b> — awaiting approval\n"
                f"{self._sub_html(run)}\n{self._status_lines(run)}{preview_line}")
        summary_md = (run.summary or "(no summary)")[:3200]
        text = (f"{head}<blockquote expandable>{md_to_telegram_html(summary_md)}"
                f"</blockquote>{self._contract_block(run)}\n"
                "Reply to this message to request changes.")
        if len(text) > 4000:
            text = (f"{head}\n{html.escape(run.summary or '')[:3200]}\n"
                    "Reply to this message to request changes.")
        try:
            r = await self._http.post("/sendMessage", json=_with_thread({
                "chat_id": self.chat_id, "text": text[:4000],
                "parse_mode": "HTML", "disable_web_page_preview": True,
                "reply_markup": approve_kb(run.id),
            }, run.tg_thread_id))
            r.raise_for_status()
            return int(r.json()["result"]["message_id"])
        except Exception:
            log.warning("approval message send failed for run=%s", run.id, exc_info=True)
            return None

    async def notify_cancelled(self, run: Run, note: str = "") -> None:
        t = run_title(run)
        suffix = f"\n{note}" if note else ""
        markdown = f"🚫 **{t}** — cancelled\n{self._sub_md(run)}{suffix}"
        await self.send_rich_markdown(
            markdown,
            fallback_html=(f"🚫 <b>{html.escape(t)}</b> — cancelled\n"
                           f"{self._sub_html(run)}{html.escape(suffix)}"),
            thread_id=run.tg_thread_id, reply_markup=restart_kb(run.id))

    # -- inbound-control plumbing --------------------------------------------

    async def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            await self._http.post("/answerCallbackQuery", json={
                "callback_query_id": callback_id, "text": text[:200]})
        except Exception:
            log.warning("answerCallbackQuery failed", exc_info=True)

    async def set_buttons(self, message_id: int, markup: dict) -> None:
        """Repaint one message's keyboard. "not modified" is the expected reply
        whenever the gate state has not moved, so it is not logged as trouble."""
        try:
            r = await self._http.post("/editMessageReplyMarkup", json={
                "chat_id": self.chat_id, "message_id": message_id,
                "reply_markup": markup})
            if r.status_code == 400 and "not modified" in r.text:
                return
            r.raise_for_status()
        except Exception:
            log.warning("set_buttons failed for message=%s", message_id,
                        exc_info=True)

    async def clear_buttons(self, message_id: int) -> None:
        try:
            await self._http.post("/editMessageReplyMarkup", json={
                "chat_id": self.chat_id, "message_id": message_id,
                "reply_markup": {"inline_keyboard": []}})
        except Exception:
            log.warning("clear_buttons failed for message=%s", message_id, exc_info=True)

    async def set_webhook(self, url: str, secret: str) -> None:
        try:
            r = await self._http.post("/setWebhook", json={
                "url": url, "secret_token": secret,
                "allowed_updates": ["callback_query", "message"]})
            r.raise_for_status()
        except Exception:
            log.warning("setWebhook failed", exc_info=True)
