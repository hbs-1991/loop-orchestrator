import json

import httpx
import respx

from loop_orchestrator.clients.telegram import (
    TelegramNotifier,
    approve_kb,
    cancel_kb,
    merge_kb,
    restart_kb,
)
from loop_orchestrator.models import Run


def make_run() -> Run:
    return Run(id=7, repo="o/r", pr_number=3, head_branch="b", state="queued",
               timeout_minutes=180, summary="сводка <b>", error="ошибка")


def mock_rich_unsupported():
    """Bot API without sendRichMessage (or a rejected markdown) -> 404/400."""
    return respx.post("https://api.telegram.org/botTOK/sendRichMessage").mock(
        return_value=httpx.Response(404, json={"ok": False}))


@respx.mock
async def test_notify_done_prefers_rich_markdown():
    rich = respx.post("https://api.telegram.org/botTOK/sendRichMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    plain = respx.post("https://api.telegram.org/botTOK/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    run = make_run()
    run.review_status = "clean"
    run.review_iteration = 1
    run.summary = "## Итог\n\n| Commit | Content |\n|---|---|\n| a1 | Task one |"
    await TelegramNotifier("TOK", 42).notify_done(run)
    assert rich.call_count == 1 and plain.call_count == 0
    payload = json.loads(rich.calls[0].request.content)
    assert payload["chat_id"] == 42
    md = payload["rich_message"]["markdown"]
    # Raw markdown passes through untouched — Telegram renders it natively.
    assert "## Итог" in md and "| a1 | Task one |" in md
    assert "[o/r#3](https://github.com/o/r/pull/3)" in md
    assert "Review: ✅ clean (1 fix iteration(s))" in md
    assert "<blockquote" not in md and "&lt;" not in md


@respx.mock
async def test_notify_done_falls_back_to_html_when_rich_rejected():
    mock_rich_unsupported()
    plain = respx.post("https://api.telegram.org/botTOK/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    run = make_run()
    run.summary = "## Резюме\n\n**Тесты:** `10 passed`"
    await TelegramNotifier("TOK", 42).notify_done(run)
    assert plain.call_count == 1
    payload = json.loads(plain.calls[0].request.content)
    assert payload["parse_mode"] == "HTML"
    assert "<b>Резюме</b>" in payload["text"]
    assert "<blockquote expandable>" in payload["text"]


@respx.mock
async def test_all_notifications_prefer_rich():
    rich = respx.post("https://api.telegram.org/botTOK/sendRichMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    plain = respx.post("https://api.telegram.org/botTOK/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    tg = TelegramNotifier("TOK", 42)
    run = make_run()
    await tg.notify_failed(run)
    run.review_iteration = 2
    await tg.notify_review_escalation(run, remaining=3)
    assert rich.call_count == 2 and plain.call_count == 0
    mds = [json.loads(c.request.content)["rich_message"]["markdown"] for c in rich.calls]
    assert "```" in mds[0] and "ошибка" in mds[0]  # error in a fenced block
    assert "[o/r#3](https://github.com/o/r/pull/3)" in mds[0]
    assert "3 finding(s) remain" in mds[1]


@respx.mock
async def test_send_and_notifications():
    mock_rich_unsupported()
    route = respx.post("https://api.telegram.org/botTOK/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    tg = TelegramNotifier("TOK", 42)
    run = make_run()
    await tg.notify_done(run)
    await tg.notify_failed(run)
    assert route.call_count == 2
    done_payload = json.loads(route.calls[0].request.content)
    assert "o/r" in done_payload["text"] and "#3" in done_payload["text"]
    assert "&lt;b&gt;" in done_payload["text"]  # HTML escaping of the agent summary
    assert "<blockquote expandable>" in done_payload["text"]
    failed_payload = json.loads(route.calls[1].request.content)
    assert "<blockquote>" in failed_payload["text"]


@respx.mock
async def test_notify_done_renders_markdown_summary():
    mock_rich_unsupported()
    route = respx.post("https://api.telegram.org/botTOK/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    run = make_run()
    run.summary = "## Резюме\n\n**Тесты:** `10 passed`\n- всё зелёное"
    await TelegramNotifier("TOK", 42).notify_done(run)
    text = json.loads(route.calls[0].request.content)["text"]
    assert "<b>Резюме</b>" in text
    assert "<code>10 passed</code>" in text
    assert "• всё зелёное" in text
    assert "##" not in text


@respx.mock
async def test_notify_done_long_summary_falls_back_to_plain():
    mock_rich_unsupported()
    route = respx.post("https://api.telegram.org/botTOK/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    run = make_run()
    # "<" escapes into 4 characters: the rich version is guaranteed to exceed the limit
    run.summary = "<" * 5000
    await TelegramNotifier("TOK", 42).notify_done(run)
    text = json.loads(route.calls[0].request.content)["text"]
    assert len(text) <= 4000
    assert "<blockquote" not in text  # flat fallback, no markup tags


@respx.mock
async def test_notify_done_includes_review_line():
    mock_rich_unsupported()
    route = respx.post("https://api.telegram.org/botTOK/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    run = make_run()
    run.review_status = "clean"
    run.review_iteration = 1
    await TelegramNotifier("TOK", 42).notify_done(run)
    text = json.loads(route.calls[0].request.content)["text"]
    assert "Review: ✅ clean (1 fix iteration(s))" in text


@respx.mock
async def test_notify_review_escalation():
    mock_rich_unsupported()
    route = respx.post("https://api.telegram.org/botTOK/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    run = make_run()
    run.review_iteration = 2
    await TelegramNotifier("TOK", 42).notify_review_escalation(run, remaining=3)
    text = json.loads(route.calls[0].request.content)["text"]
    assert "not clean after 2 fix iteration(s)" in text
    assert "3 finding(s) remain" in text


@respx.mock
async def test_send_video_mp4_uses_sendvideo():
    route = respx.post("https://api.telegram.org/bottok/sendVideo").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    await tg.send_video(b"\x00vid", "main.mp4", "Run #1 e2e")
    assert route.called
    body = route.calls[0].request.content
    assert b"main.mp4" in body
    await tg.aclose()


@respx.mock
async def test_send_video_webm_uses_senddocument():
    route = respx.post("https://api.telegram.org/bottok/sendDocument").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    await tg.send_video(b"\x00vid", "fail-1.webm", "Run #1 e2e failure")
    assert route.called
    await tg.aclose()


@respx.mock
async def test_send_video_falls_back_to_text():
    respx.post("https://api.telegram.org/bottok/sendVideo").respond(500)
    fallback = respx.post("https://api.telegram.org/bottok/sendMessage").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    await tg.send_video(b"\x00vid", "main.mp4", "Run #1 e2e")
    assert fallback.called
    await tg.aclose()


@respx.mock
async def test_notify_done_mentions_e2e():
    route = respx.post("https://api.telegram.org/bottok/sendRichMessage").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=7, repo="o/r", pr_number=1, head_branch="b", state="reporting",
              summary="done", e2e_status="passed", e2e_iteration=1)
    await tg.notify_done(run)
    md = json.loads(route.calls[0].request.content)["rich_message"]["markdown"]
    assert "E2E: ✅ passed (1 fix iteration(s)) 🎬" in md
    await tg.aclose()


@respx.mock
async def test_notify_e2e_escalation():
    route = respx.post("https://api.telegram.org/bottok/sendRichMessage").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=7, repo="o/r", pr_number=1, head_branch="b", state="reporting",
              e2e_status="escalated", e2e_iteration=2)
    await tg.notify_e2e_escalation(run, 3)
    sent = route.calls[0].request.content.decode()
    assert "3" in sent and "e2e" in sent.lower()
    await tg.aclose()


async def test_aclose_closes_only_owned_client():
    tg = TelegramNotifier("TOK", 1)
    await tg.aclose()
    assert tg._http.is_closed
    injected = httpx.AsyncClient()
    tg2 = TelegramNotifier("TOK", 1, client=injected)
    await tg2.aclose()
    assert not injected.is_closed  # injected client belongs to the caller
    await injected.aclose()


@respx.mock
async def test_send_passes_thread_id():
    route = respx.post("https://api.telegram.org/bottok/sendMessage").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    await tg.send("hello", thread_id=777)
    body = json.loads(route.calls[0].request.content)
    assert body["message_thread_id"] == 777


@respx.mock
async def test_rich_passes_thread_id_and_falls_back_with_it():
    respx.post("https://api.telegram.org/bottok/sendRichMessage").respond(500)
    fallback = respx.post("https://api.telegram.org/bottok/sendMessage").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    await tg.send_rich_markdown("hi", fallback_html="hi", thread_id=777)
    body = json.loads(fallback.calls[0].request.content)
    assert body["message_thread_id"] == 777


@respx.mock
async def test_send_video_passes_thread_id():
    route = respx.post("https://api.telegram.org/bottok/sendVideo").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    await tg.send_video(b"v", "main.mp4", "cap", thread_id=777)
    assert b"777" in route.calls[0].request.content


@respx.mock
async def test_send_card_is_silent_and_returns_message_id():
    route = respx.post("https://api.telegram.org/bottok/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 555}})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=8, repo="o/r", pr_number=7, head_branch="b", state="queued",
              pr_title="feat: x", tg_thread_id=777)
    mid = await tg.send_card(run, [("queued", "2026-08-01 07:01:00")])
    assert mid == 555
    body = json.loads(route.calls[0].request.content)
    assert body["disable_notification"] is True
    assert body["message_thread_id"] == 777
    assert "feat: x" in body["text"]


@respx.mock
async def test_send_card_failure_degrades_to_none():
    respx.post("https://api.telegram.org/bottok/sendMessage").respond(400)
    tg = TelegramNotifier("tok", 42)
    run = Run(id=8, repo="o/r", pr_number=7, head_branch="b", state="queued")
    assert await tg.send_card(run, []) is None


@respx.mock
async def test_update_card_edits_message():
    route = respx.post("https://api.telegram.org/bottok/editMessageText").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=8, repo="o/r", pr_number=7, head_branch="b", state="executing",
              tg_card_message_id=555)
    await tg.update_card(run, [("queued", "2026-08-01 07:01:00")])
    body = json.loads(route.calls[0].request.content)
    assert body["message_id"] == 555


@respx.mock
async def test_update_card_ignores_not_modified_and_absence():
    respx.post("https://api.telegram.org/bottok/editMessageText").respond(
        400, json={"ok": False, "description": "Bad Request: message is not modified"})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=8, repo="o/r", pr_number=7, head_branch="b", state="executing",
              tg_card_message_id=555)
    await tg.update_card(run, [])          # must not raise
    run.tg_card_message_id = None
    await tg.update_card(run, [])          # no card -> silent no-op


@respx.mock
async def test_start_and_finish_run_thread():
    respx.post("https://api.telegram.org/bottok/createForumTopic").respond(
        200, json={"ok": True, "result": {"message_thread_id": 777}})
    rename = respx.post("https://api.telegram.org/bottok/editForumTopic").respond(
        200, json={"ok": True, "result": True})
    close = respx.post("https://api.telegram.org/bottok/closeForumTopic").respond(
        200, json={"ok": True, "result": True})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=8, repo="o/r", pr_number=7, head_branch="b", state="queued",
              pr_title="feat: x")
    assert await tg.start_run_thread(run) == 777
    run.state, run.tg_thread_id = "done", 777
    await tg.finish_run_thread(run)
    assert rename.called and close.called
    sent = json.loads(rename.calls[0].request.content)
    assert sent["name"].startswith("✅")


@respx.mock
async def test_finish_run_thread_without_thread_is_noop():
    tg = TelegramNotifier("tok", 42)
    run = Run(id=8, repo="o/r", pr_number=7, head_branch="b", state="done")
    await tg.finish_run_thread(run)  # no respx routes -> would raise if it called out


@respx.mock
async def test_notify_done_uses_feature_title_and_thread():
    route = respx.post("https://api.telegram.org/bottok/sendRichMessage").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=8, repo="o/r", pr_number=7, head_branch="b", state="reporting",
              pr_title="feat: web playground", tg_thread_id=777,
              summary="done", review_status="clean", review_iteration=1,
              e2e_status="passed", e2e_iteration=0)
    await tg.notify_done(run)
    body = json.loads(route.calls[0].request.content)
    assert body["message_thread_id"] == 777
    text = body["rich_message"]["markdown"]
    assert "feat: web playground" in text and "finished" in text
    assert "Review: ✅ clean (1 fix iteration(s))" in text
    assert "E2E: ✅ passed (0 fix iteration(s)) 🎬" in text


@respx.mock
async def test_notify_failed_uses_feature_title():
    route = respx.post("https://api.telegram.org/bottok/sendRichMessage").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=8, repo="o/r", pr_number=7, head_branch="b", state="failed",
              pr_title="feat: x", error="[executing] boom")
    await tg.notify_failed(run)
    text = json.loads(route.calls[0].request.content)["rich_message"]["markdown"]
    assert "❌ **feat: x** — failed" in text and "boom" in text


@respx.mock
async def test_notify_failed_planning_run_links_issue():
    route = respx.post("https://api.telegram.org/bottok/sendRichMessage").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=8, repo="o/r", pr_number=0, head_branch="loop/issue-9",
              state="failed", pr_title="feat: x", error="boom",
              kind="planning", issue_number=9)
    await tg.notify_failed(run)
    text = json.loads(route.calls[0].request.content)["rich_message"]["markdown"]
    assert "https://github.com/o/r/issues/9" in text and "o/r#9" in text
    assert "pull/0" not in text


# -- phase 4a: keyboards, approval message, callback plumbing -----------------


def _payload(route, i=0):
    return json.loads(route.calls[i].request.content)


@respx.mock
async def test_notify_awaiting_approval_buttons_and_id():
    route = respx.post("https://api.telegram.org/bottok/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 321}})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=9, repo="o/r", pr_number=3, head_branch="b",
              state="awaiting_approval", pr_title="feat: x",
              summary="did things", preview_url="https://s-a-3000.preview.x",
              tg_thread_id=777)
    msg_id = await tg.notify_awaiting_approval(run)
    assert msg_id == 321
    body = _payload(route)
    assert body["message_thread_id"] == 777
    assert "preview" in body["text"] and "Reply to this message" in body["text"]
    assert body["reply_markup"] == approve_kb(9)
    assert "disable_notification" not in body  # this one must push


@respx.mock
async def test_notify_awaiting_approval_failure_degrades_to_none():
    respx.post("https://api.telegram.org/bottok/sendMessage").respond(400)
    tg = TelegramNotifier("tok", 42)
    run = Run(id=9, repo="o/r", pr_number=3, head_branch="b",
              state="awaiting_approval")
    assert await tg.notify_awaiting_approval(run) is None


@respx.mock
async def test_notify_cancelled_carries_restart_button():
    route = respx.post("https://api.telegram.org/bottok/sendRichMessage").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=9, repo="o/r", pr_number=3, head_branch="b", state="cancelled",
              pr_title="feat: x", tg_thread_id=777)
    await tg.notify_cancelled(run, "cancelled by 1")
    body = _payload(route)
    assert body["reply_markup"] == restart_kb(9)
    md = body["rich_message"]["markdown"]
    assert "🚫 **feat: x** — cancelled" in md and "cancelled by 1" in md


@respx.mock
async def test_done_and_failed_carry_buttons_through_the_fallback():
    mock_rich_unsupported()
    route = respx.post("https://api.telegram.org/bottok/sendMessage").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    run = make_run()
    await tg.notify_done(run)
    await tg.notify_failed(run)
    assert _payload(route, 0)["reply_markup"] == merge_kb(7)
    assert _payload(route, 1)["reply_markup"] == restart_kb(7)


@respx.mock
async def test_card_carries_cancel_button_while_cancelable():
    route = respx.post("https://api.telegram.org/bottok/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 1}})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=9, repo="o/r", pr_number=3, head_branch="b", state="executing")
    await tg.send_card(run, [])
    assert _payload(route)["reply_markup"] == cancel_kb(9)
    # terminal/paused states get no cancel button
    run.state = "awaiting_approval"
    run.tg_card_message_id = 1
    edit = respx.post("https://api.telegram.org/bottok/editMessageText").respond(
        200, json={"ok": True})
    await tg.update_card(run, [])
    assert "reply_markup" not in _payload(edit)


@respx.mock
async def test_update_card_carries_cancel_button_while_cancelable():
    edit = respx.post("https://api.telegram.org/bottok/editMessageText").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    run = Run(id=9, repo="o/r", pr_number=3, head_branch="b", state="executing",
              tg_card_message_id=1)
    await tg.update_card(run, [])
    assert _payload(edit)["reply_markup"] == cancel_kb(9)


def test_keyboard_helpers_wire_format():
    assert approve_kb(3)["inline_keyboard"][0][0]["callback_data"] == "ap:3"
    assert approve_kb(3)["inline_keyboard"][0][1]["callback_data"] == "dc:3"
    assert cancel_kb(3)["inline_keyboard"][0][0]["callback_data"] == "cn:3"
    assert merge_kb(3)["inline_keyboard"][0][0]["callback_data"] == "mg:3"
    assert merge_kb(3)["inline_keyboard"][0][1]["callback_data"] == "md:3"
    assert restart_kb(3)["inline_keyboard"][0][0]["callback_data"] == "rs:3"


@respx.mock
async def test_answer_callback_and_clear_buttons_swallow_errors():
    respx.post("https://api.telegram.org/bottok/answerCallbackQuery").respond(
        400, json={"ok": False})
    respx.post("https://api.telegram.org/bottok/editMessageReplyMarkup").respond(
        400, json={"ok": False})
    tg = TelegramNotifier("tok", 42)
    await tg.answer_callback("cbid", "hi")   # must not raise
    await tg.clear_buttons(55)               # must not raise


@respx.mock
async def test_answer_callback_and_clear_buttons_payloads():
    cb = respx.post("https://api.telegram.org/bottok/answerCallbackQuery").respond(
        200, json={"ok": True})
    clear = respx.post("https://api.telegram.org/bottok/editMessageReplyMarkup").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    await tg.answer_callback("cbid", "approved")
    await tg.clear_buttons(55)
    assert _payload(cb) == {"callback_query_id": "cbid", "text": "approved"}
    body = _payload(clear)
    assert body["message_id"] == 55
    assert body["reply_markup"] == {"inline_keyboard": []}


@respx.mock
async def test_set_webhook():
    route = respx.post("https://api.telegram.org/bottok/setWebhook").respond(
        200, json={"ok": True})
    tg = TelegramNotifier("tok", 42)
    await tg.set_webhook("https://loop.example.com/webhooks/telegram", "s3cret")
    body = _payload(route)
    assert body["secret_token"] == "s3cret"
    assert body["url"].endswith("/webhooks/telegram")
    assert "callback_query" in body["allowed_updates"]


@respx.mock
async def test_set_webhook_swallows_errors():
    respx.post("https://api.telegram.org/bottok/setWebhook").respond(400)
    await TelegramNotifier("tok", 42).set_webhook("https://x/y", "s")
