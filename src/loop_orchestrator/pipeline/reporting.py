"""How a Run tells the world what happened: labels, PR comments, Telegram.

Everything here is best-effort by construction — a Run that finished its work
must not be recorded as failed because a label call timed out — with one
exception: `fail` is the single funnel every failure path ends in, so it runs
its notifications one by one and swallows each independently.
"""

import json

from .. import db as dbmod
from .. import issue_tasks as it
from ..e2e import format_e2e_comment
from ..models import CANCELLED, FAILED, Run
from ..review import format_review_comment
from ..state_machine import InvalidTransition, transition


class ReportingMixin:
    async def _refresh_card(self, run: Run) -> None:
        """Best-effort card repaint; notification failures never fail the run."""
        try:
            events = await dbmod.events_for_run(self.db, run.id)
            await self.tg.update_card(run, events)
        except Exception:  # noqa: BLE001
            pass

    # A run has exactly one outcome, so a PR wears exactly one of these. Both
    # halves of that invariant were broken live: PR #16 carried loop:run next
    # to the previous pass's loop:done, and PR #13 ended up with loop:failed
    # and loop:done at once after a failed run was resumed and finished.
    _VERDICT_LABELS = ("loop:done", "loop:needs-review", "loop:failed")

    async def _set_verdict_label(self, repo: str, number: int,
                                 label: str | None) -> None:
        """Make `label` the only loop verdict on the PR/issue (None = clear)."""
        for stale in self._VERDICT_LABELS:
            if stale == label:
                continue
            try:
                await self.gh.remove_label(repo, number, stale)
            except Exception:  # noqa: BLE001 — a label that isn't there is fine
                pass
        if label:
            await self.gh.add_labels(repo, number, [label])

    async def _swap_labels_start(self, run: Run) -> None:
        await self.gh.ensure_labels(run.repo)
        await self.gh.remove_label(run.repo, run.pr_number, "loop:run")
        await self._set_verdict_label(run.repo, run.pr_number, None)
        await self.gh.add_labels(run.repo, run.pr_number, ["loop:running"])

    async def _report_success(self, run: Run) -> None:
        escalated = (run.review_status == "escalated"
                     or run.e2e_status == "escalated")
        await self.gh.remove_label(run.repo, run.pr_number, "loop:running")
        # Replaces any earlier verdict: a run that failed, was resumed and then
        # finished must not leave loop:failed sitting next to loop:done.
        await self._set_verdict_label(
            run.repo, run.pr_number,
            "loop:needs-review" if escalated else "loop:done")
        await self.gh.create_comment(
            run.repo, run.pr_number,
            f"✅ Loop run #{run.id} finished.\n\n{run.summary or ''}")
        if run.review_status:
            report = json.loads(run.review_json or "{}")
            await self.gh.create_comment(
                run.repo, run.pr_number,
                format_review_comment(run.review_status, run.review_iteration, report))
        if run.e2e_status:
            e2e_report = json.loads(run.e2e_json or "{}")
            await self.gh.create_comment(
                run.repo, run.pr_number,
                format_e2e_comment(run.e2e_status, run.e2e_iteration, e2e_report))
        mid = await self.tg.notify_done(run)
        if mid is not None:
            run.tg_merge_message_id = mid
            await dbmod.save_run(self.db, run)
        if run.review_status == "escalated":
            remaining = len(json.loads(run.review_json or "{}").get("remaining", []))
            await self.tg.notify_review_escalation(run, remaining)
        if run.e2e_status == "escalated":
            failing = sum(1 for t in json.loads(run.e2e_json or "{}").get("tests", [])
                          if t.get("status") == "failed")
            await self.tg.notify_e2e_escalation(run, failing)
        # Paused runs already got their videos with the approval request.
        if run.tg_approval_message_id is None:
            await self._send_e2e_videos(run)

    async def _report_planning(self, run: Run) -> None:
        if run.pr_number == 0:  # questions outcome — no PR was created
            await self.gh.create_comment(
                run.repo, run.issue_number,
                "❓ Loop planner needs more information before it can plan "
                "this issue. Please answer in a comment; the task will resume "
                f"automatically.\n\n{run.summary or ''}")
            await it.set_state(self.db, run.repo, run.issue_number,
                               it.NEEDS_INFO)
            await self.tg.send(
                f"❓ Issue #{run.issue_number} ({run.repo}): the planner needs "
                "more information — reply in the issue to resume.",
                thread_id=run.tg_thread_id)
            return
        await self.gh.ensure_labels(run.repo)
        await self.gh.create_comment(
            run.repo, run.issue_number,
            f"🧭 Plan for issue #{run.issue_number} is ready and approved by "
            f"the Implementor Advisor: see PR #{run.pr_number}. "
            "Execution starts automatically.")
        await self.tg.send(
            f"🧭 Issue #{run.issue_number}: plan approved — PR "
            f"#{run.pr_number} queued for execution.",
            thread_id=run.tg_thread_id)
        await self.gh.add_labels(run.repo, run.pr_number, ["loop:run"])

    async def fail(self, run: Run, stage: str, message: str) -> None:
        fresh = await dbmod.get_run(self.db, run.id)
        if fresh is not None and fresh.state == CANCELLED:
            return  # a concurrent cancel/discard won — not a failure
        run.error = f"[{stage}] {message}"
        try:
            await transition(self.db, run, FAILED, detail=run.error)
        except InvalidTransition:
            run.state = FAILED
            await dbmod.save_run(self.db, run)
        # Planning runs live on the issue, not on a PR (which may not exist yet).
        number = run.pr_number if run.kind == "pr" else run.issue_number
        actions = [
            lambda: self._set_verdict_label(run.repo, number, "loop:failed"),
            lambda: self.gh.create_comment(
                run.repo, number, f"❌ Loop run #{run.id} failed: {run.error}"),
            lambda: self.tg.notify_failed(run),
            lambda: self._refresh_card(run),
            lambda: self.tg.finish_run_thread(run),
        ]
        if run.kind == "pr":  # planning runs never set loop:running
            actions.insert(
                0, lambda: self.gh.remove_label(run.repo, number, "loop:running"))
        for action in actions:
            try:
                await action()
            except Exception:  # noqa: BLE001 — reporting must not fail as a whole
                pass
