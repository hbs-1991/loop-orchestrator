"""Progress-card rendering: one HTML checklist message edited in place.

Pure functions — no I/O — so every state combination is unit-testable.
"""
import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..models import (
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

STAGES = (QUEUED, PREPARING, EXECUTING, REVIEWING, E2E_TESTING,
          STAGING, AWAITING_APPROVAL, PUBLISHING, REPORTING)
PLANNING_STAGES = (QUEUED, PREPARING, PLANNING, PUBLISHING, REPORTING)
_LABELS = {QUEUED: "queued", PREPARING: "preparing", EXECUTING: "executing",
           REVIEWING: "reviewing", E2E_TESTING: "e2e testing",
           STAGING: "staging", AWAITING_APPROVAL: "awaiting approval",
           PUBLISHING: "publishing", REPORTING: "reporting",
           PLANNING: "planning"}


def _stages_for(run: Run) -> tuple[str, ...]:
    return PLANNING_STAGES if run.kind == "planning" else STAGES


def _ref(run: Run) -> tuple[str, str]:
    """(url, display-ref) — PR for pr-runs, issue for planning runs."""
    if run.kind == "planning":
        return (f"https://github.com/{run.repo}/issues/{run.issue_number}",
                f"{run.repo}#{run.issue_number}")
    return (f"https://github.com/{run.repo}/pull/{run.pr_number}",
            f"{run.repo}#{run.pr_number}")


def run_title(run: Run) -> str:
    return run.pr_title or f"Run #{run.id}"


def _header_emoji(run: Run) -> str:
    if run.state == FAILED:
        return "❌"
    if run.state == CANCELLED:
        return "🚫"
    if run.state == DONE:
        if run.merged_at:
            return "🔀"
        if run.review_status == "escalated" or run.e2e_status == "escalated":
            return "⚠️"
        return "✅"
    return "🌀"


def _topic_number(run: Run) -> int:
    return run.issue_number if run.kind == "planning" else run.pr_number


def topic_name(run: Run) -> str:
    return f"⏳ {run_title(run)} · #{_topic_number(run)}"


def topic_final_name(run: Run) -> str:
    return f"{_header_emoji(run)} {run_title(run)} · #{_topic_number(run)}"


def _fmt_time(created_at: str, tz: str) -> str:
    try:
        zone = ZoneInfo(tz)
    except Exception:  # unknown zone name — fall back to UTC
        zone = timezone.utc
    dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(zone).strftime("%H:%M")


def render_card(run: Run, events: list[tuple[str, str]], tz: str) -> str:
    """HTML for the progress message; `events` is (to_state, created_at UTC).

    A revise restarts the verification cycle, so stage times reset at the
    latest `executing` event: the card tracks the current cycle, while
    queued/preparing keep their original (one-off) times.
    """
    last_exec: int | None = None
    revisions = -1
    for i, (state, _) in enumerate(events):
        if state == EXECUTING:
            last_exec = i
            revisions += 1
    times: dict[str, str] = {}
    for i, (state, created) in enumerate(events):
        if state in (QUEUED, PREPARING) or last_exec is None or i >= last_exec:
            times.setdefault(state, created)
    stages = _stages_for(run)
    reached = [s for s in stages if s in times]
    last = reached[-1] if reached else QUEUED
    prepared = EXECUTING in times  # review/e2e flags are meaningful after prepare
    lines = []
    for stage in stages:
        if run.state == FAILED and stage == last:
            icon = "⛔"
        elif run.state == CANCELLED and stage == last:
            icon = "🚫"
        elif stage == run.state:
            icon = "⏳"
        elif stage in times:
            icon = "✅"
        elif prepared and stage == REVIEWING and not run.review_enabled:
            icon = "➖"
        elif prepared and stage == E2E_TESTING and not run.e2e_enabled:
            icon = "➖"
        elif prepared and stage == AWAITING_APPROVAL and run.approval_mode == "never":
            icon = "➖"
        else:
            icon = "⬜"
        t = f"  {_fmt_time(times[stage], tz)}" if stage in times else ""
        extra = ""
        if (stage == AWAITING_APPROVAL and run.state == AWAITING_APPROVAL
                and run.sandbox_expires_at):
            extra = f"  (preview until {_fmt_time(run.sandbox_expires_at, tz)})"
        lines.append(f"{icon} {_LABELS[stage]}{t}{extra}")
    url, ref = _ref(run)
    rev = f" · revision {revisions}" if revisions > 0 else ""
    head = (f"{_header_emoji(run)} <b>{html.escape(run_title(run))}</b>\n"
            f'<a href="{url}">{ref}</a> · Run {run.id}{rev}')
    return head + "\n\n" + "\n".join(lines)
