"""Progress-card rendering: icons per stage, times, titles, topic names."""
from zoneinfo import ZoneInfo

import pytest

from loop_orchestrator.clients.tg_card import (
    render_card,
    run_title,
    topic_final_name,
    topic_name,
)
from loop_orchestrator.models import (
    AWAITING_APPROVAL,
    CANCELLED,
    CONTRACTING,
    EXECUTING,
    PREPARING,
    QUEUED,
    STAGING,
    Run,
)


def make_run(**kw) -> Run:
    base = dict(id=8, repo="o/r", pr_number=7, head_branch="b", state="executing",
                pr_title="feat: web playground")
    base.update(kw)
    return Run(**base)


EVENTS = [("queued", "2026-08-01 07:01:00"), ("preparing", "2026-08-01 07:01:30"),
          ("executing", "2026-08-01 07:02:00")]


def _has_tz_database() -> bool:
    """The IANA database ships with the OS on Linux but not on Windows."""
    try:
        ZoneInfo("Etc/GMT-5")
    except Exception:
        return False
    return True


def test_run_title_falls_back_to_run_id():
    assert run_title(make_run()) == "feat: web playground"
    assert run_title(make_run(pr_title=None)) == "Run #8"


def test_card_running_marks_past_current_future():
    card = render_card(make_run(), EVENTS, "UTC")
    assert "<b>feat: web playground</b>" in card
    assert 'href="https://github.com/o/r/pull/7"' in card
    assert "· Run 8" in card
    assert "✅ queued" in card and "07:01" in card
    assert "⏳ executing" in card
    assert "⬜ publishing" in card and "⬜ reporting" in card


@pytest.mark.skipif(not _has_tz_database(), reason="no IANA time zone database on this host")
def test_card_times_respect_tz():
    card = render_card(make_run(), EVENTS, "Etc/GMT-5")  # UTC+5
    assert "12:01" in card


def test_card_bad_tz_falls_back_to_utc():
    card = render_card(make_run(), EVENTS, "No/Such_Zone")
    assert "07:01" in card


def test_card_skipped_stages_after_prepare():
    run = make_run(review_enabled=False, e2e_enabled=False)
    card = render_card(run, EVENTS, "UTC")
    assert "➖ reviewing" in card
    assert "➖ e2e testing" in card


def test_card_before_prepare_shows_future_not_skipped():
    run = make_run(state="queued", review_enabled=True, e2e_enabled=False)
    card = render_card(run, [("queued", "2026-08-01 07:01:00")], "UTC")
    assert "➖" not in card
    assert "⏳ queued" in card


def test_card_failed_marks_last_stage():
    run = make_run(state="failed", e2e_enabled=True)
    card = render_card(run, EVENTS, "UTC")
    assert card.startswith("❌")
    assert "⛔ executing" in card
    assert "✅ preparing" in card


def test_card_done_all_green():
    events = EVENTS + [("reviewing", "2026-08-01 07:10:00"),
                       ("e2e_testing", "2026-08-01 07:15:00"),
                       ("staging", "2026-08-01 07:16:00"),
                       ("awaiting_approval", "2026-08-01 07:17:00"),
                       ("publishing", "2026-08-01 07:20:00"),
                       ("reporting", "2026-08-01 07:21:00")]
    run = make_run(state="done", e2e_enabled=True)
    card = render_card(run, events, "UTC")
    assert card.startswith("✅")
    assert "⏳" not in card and "⬜" not in card


def test_card_done_escalated_header():
    run = make_run(state="done", review_status="escalated")
    assert render_card(run, EVENTS, "UTC").startswith("⚠️")


def test_card_escapes_html_in_title():
    run = make_run(pr_title="feat: a <b> & c")
    card = render_card(run, EVENTS, "UTC")
    assert "a &lt;b&gt; &amp; c" in card


def test_topic_names():
    run = make_run()
    assert topic_name(run) == "⏳ feat: web playground · #7"
    assert topic_final_name(make_run(state="done")) == "✅ feat: web playground · #7"
    assert topic_final_name(make_run(state="failed")) == "❌ feat: web playground · #7"
    assert topic_final_name(
        make_run(state="done", e2e_status="escalated")) == "⚠️ feat: web playground · #7"
    assert topic_final_name(
        make_run(state="done", merged_at="2026-08-03 10:00:00")
    ) == "🔀 feat: web playground · #7"


def _run4a(state, **kw):
    return Run(id=9, repo="o/r", pr_number=3, head_branch="b", state=state,
               pr_title="feat: x", **kw)


def test_card_awaiting_approval_shows_deadline():
    run = _run4a(AWAITING_APPROVAL, sandbox_expires_at="2026-08-03 10:30:00")
    events = [("queued", "2026-08-03 08:00:00"), ("staging", "2026-08-03 09:00:00"),
              (AWAITING_APPROVAL, "2026-08-03 09:01:00")]
    card = render_card(run, events, "UTC")
    assert "⏳ awaiting approval" in card
    assert "preview until 10:30" in card
    assert "✅ staging" in card


def test_card_skips_pause_when_approval_never():
    run = _run4a("publishing", approval_mode="never")
    events = [("queued", "2026-08-03 08:00:00"), ("executing", "2026-08-03 08:01:00"),
              ("staging", "2026-08-03 08:30:00"), ("publishing", "2026-08-03 08:31:00")]
    card = render_card(run, events, "UTC")
    assert "➖ awaiting approval" in card


def test_card_cancelled_header_and_topic():
    run = _run4a(CANCELLED)
    events = [("queued", "2026-08-03 08:00:00"), ("executing", "2026-08-03 08:01:00"),
              (CANCELLED, "2026-08-03 08:10:00")]
    card = render_card(run, events, "UTC")
    assert card.startswith("🚫")
    assert "🚫 executing" in card  # the stage it died on
    assert topic_final_name(run).startswith("🚫")


REVISE_EVENTS = [
    ("queued", "2026-08-03 08:00:00"), ("preparing", "2026-08-03 08:00:30"),
    ("executing", "2026-08-03 08:01:00"), ("reviewing", "2026-08-03 08:05:00"),
    ("e2e_testing", "2026-08-03 08:10:00"), ("staging", "2026-08-03 08:15:00"),
    ("awaiting_approval", "2026-08-03 08:16:00"),
    ("executing", "2026-08-03 08:30:00"),   # revise restarts the cycle
]


def test_card_revise_restarts_stage_times():
    run = make_run(state="executing", review_enabled=True, e2e_enabled=True)
    card = render_card(run, REVISE_EVENTS, "UTC")
    assert "⏳ executing  08:30" in card         # the new cycle's time
    assert "✅ queued" in card and "✅ preparing" in card
    # stages of the previous cycle are upcoming again, not done
    for label in ("reviewing", "e2e testing", "staging", "awaiting approval"):
        assert f"⬜ {label}" in card
        assert f"✅ {label}" not in card
    assert "revision 1" in card


def test_card_revise_cycle_progresses():
    run = make_run(state="reviewing")
    events = REVISE_EVENTS + [("reviewing", "2026-08-03 08:33:00")]
    card = render_card(run, events, "UTC")
    assert "✅ executing  08:30" in card
    assert "⏳ reviewing  08:33" in card
    assert "⬜ staging" in card


def test_card_no_revision_suffix_on_first_cycle():
    card = render_card(make_run(), EVENTS, "UTC")
    assert "revision" not in card


def test_planning_card_shows_planning_stages_and_issue_link():
    run = Run(id=1, repo="o/r", pr_number=0, head_branch="loop/issue-7",
              state="planning", kind="planning", issue_number=7, pr_title="T")
    card = render_card(run, [("queued", "2026-08-03 10:00:00"),
                             ("preparing", "2026-08-03 10:01:00"),
                             ("planning", "2026-08-03 10:02:00")], tz="UTC")
    assert "planning" in card and "executing" not in card
    assert "https://github.com/o/r/issues/7" in card


def test_planning_topic_name_uses_issue_number():
    run = Run(id=1, repo="o/r", pr_number=0, head_branch="loop/issue-7",
              state="queued", kind="planning", issue_number=7, pr_title="T")
    assert topic_name(run) == "⏳ T · #7"


def test_card_shows_contracting_and_skips_it_when_disabled():
    run = Run(id=1, repo="o/r", pr_number=5, head_branch="b", state=STAGING)
    events = [(QUEUED, "2026-08-08 10:00:00"), (PREPARING, "2026-08-08 10:01:00"),
              (EXECUTING, "2026-08-08 10:02:00"), (STAGING, "2026-08-08 10:30:00")]
    text = render_card(run, events, "UTC")
    assert "➖ contracting" in text          # run not tied to an issue

    run.contract_enabled = True
    run.contract_status = "skipped"          # the issue blocks nobody
    assert "➖ contracting" in render_card(run, events, "UTC")

    run.contract_status = "produced"
    run.state = CONTRACTING
    assert "⏳ contracting" in render_card(run, events, "UTC")
