"""The reviewing stage: an independent reviewer, and the fix rounds it drives.

Never fatal. Whatever happens here the code still publishes — the outcome is
recorded on the Run (`review_status`, `review_json`) and read back by the
reporting stage, which is what turns an escalation into `loop:needs-review`.
"""

import json

from .. import db as dbmod
from ..models import REVIEWING, Run
from ..review import (
    Finding,
    VerdictError,
    build_fix_prompt,
    build_review_prompt,
    newly_fixed,
    parse_verdict,
    report_dict,
)
from . import clock
from .constants import MAX_TASK_TIMEOUT_S
from .errors import ReviewDeadline, ReviewTaskError


class ReviewMixin:
    async def _finish_review(self, run: Run, status: str, summary: str,
                             fixed: list[Finding], remaining: list[Finding]) -> None:
        run.review_status = status
        run.review_json = json.dumps(report_dict(summary, fixed, remaining),
                                     ensure_ascii=False)
        await dbmod.save_run(self.db, run)
        await dbmod.add_event(self.db, run.id, REVIEWING, REVIEWING,
                              f"review finished: {status}")

    async def _review(self, run: Run) -> None:
        # Reviewing gets a fresh run.timeout_minutes budget for the whole
        # review+fix cycle (execute's elapsed time is not persisted).
        deadline = clock.monotonic() + run.timeout_minutes * 60
        review_timeout_s = min(self.settings.review_timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        fix_timeout_s = min(run.timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        fixed: list[Finding] = []
        pending: list[Finding] = []
        retried = False
        while True:
            try:
                # A fresh session, always. The reviewer is meant to be
                # independent, and inheriting the executor's session made it
                # neither independent nor cheap: it started every call with
                # ~230k tokens of the executor's context, and because the model
                # changes here too the prompt cache misses outright, so that
                # whole context is re-written at 1.25x price. The prompt names
                # the spec, the plan and the branch to diff against — it needs
                # nothing the executor said.
                task, deadline = await self._run_sandbox_task(
                    run, build_review_prompt(run.spec_path, run.plan_path, run.head_branch),
                    review_timeout_s, deadline, model=self.settings.reviewer_model,
                    continue_session=False, trace_stage="review")
                verdict = parse_verdict(task.get("agent_message_final")
                                        or task.get("agent_message") or "")
            except ReviewDeadline:
                await self._finish_review(run, "escalated",
                                          "review interrupted by run timeout",
                                          fixed, pending)
                return
            except (ReviewTaskError, VerdictError) as e:
                if retried:
                    await self._finish_review(run, "skipped",
                                              f"review skipped: {e}", fixed, pending)
                    return
                retried = True
                await dbmod.add_event(self.db, run.id, REVIEWING, REVIEWING,
                                      f"review attempt failed, retrying once: {e}")
                continue
            retried = False
            fixed += newly_fixed(pending, verdict.findings)
            pending = verdict.findings
            if verdict.verdict == "clean":
                await self._finish_review(run, "clean", verdict.summary, fixed, [])
                return
            if run.review_iteration >= run.review_max_iterations:
                await self._finish_review(run, "escalated", verdict.summary,
                                          fixed, pending)
                return
            run.review_iteration += 1
            await dbmod.save_run(self.db, run)
            await dbmod.add_event(self.db, run.id, REVIEWING, REVIEWING,
                                  f"fix iteration {run.review_iteration}: "
                                  f"{len(pending)} finding(s)")
            try:
                # Fresh too: the findings carry file, line and detail, so the
                # fixer works from the verdict and the code on disk rather than
                # from the reviewer's session.
                _, deadline = await self._run_sandbox_task(
                    run, build_fix_prompt(verdict, run.test_cmd, run.head_branch,
                                          run.spec_path, run.plan_path),
                    fix_timeout_s, deadline, continue_session=False,
                    trace_stage="review-fix")
            except ReviewDeadline:
                await self._finish_review(run, "escalated",
                                          "review interrupted by run timeout",
                                          fixed, pending)
                return
            except ReviewTaskError as e:
                await self._finish_review(run, "escalated",
                                          f"fix task failed: {e}", fixed, pending)
                return
