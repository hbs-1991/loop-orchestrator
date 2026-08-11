"""The planning Run: an issue in, a spec+plan PR out.

A second, shorter state machine living beside `process` — planning Runs skip
review, e2e, contracting and the approval pause, and the gate they do have is
the Implementor Advisor rather than a human. Both agents are configurable per
repository; the knobs were snapshotted onto the Run at prepare.
"""

from .. import db as dbmod
from .. import issue_tasks as it
from ..models import DONE, PLANNING, PREPARING, PUBLISHING, QUEUED, REPORTING, Run
from ..planning import (
    PlannerResult,
    PlanningError,
    build_advisor_prompt,
    build_planner_revise_prompt,
    parse_advisor_verdict,
    parse_planner_output,
)
from ..state_machine import transition
from . import clock
from .constants import MAX_TASK_TIMEOUT_S
from .errors import ReviewDeadline, ReviewTaskError, RunFailure


class PlanningMixin:
    async def process_planning(self, run: Run) -> None:
        try:
            if run.state == QUEUED:
                if run.tg_thread_id is None:
                    run.tg_thread_id = await self.tg.start_run_thread(run)
                    await it.set_topic(self.db, run.repo, run.issue_number,
                                       run.tg_thread_id)
                if run.tg_card_message_id is None:
                    events = await dbmod.events_for_run(self.db, run.id)
                    run.tg_card_message_id = await self.tg.send_card(run, events)
                await dbmod.save_run(self.db, run)
                await transition(self.db, run, PREPARING)
                await self._refresh_card(run)
            if run.state == PREPARING:
                await self._prepare_planning(run)
                await transition(self.db, run, PLANNING)
                await self._refresh_card(run)
            if run.state == PLANNING:
                result = await self._planning(run)
                run.summary = (result.summary or
                               "\n".join(f"- {q}" for q in result.questions))
                await dbmod.save_run(self.db, run)
                if result.outcome == "questions":
                    await transition(self.db, run, REPORTING,
                                     detail="questions for the issue author")
                else:
                    await transition(self.db, run, PUBLISHING)
                await self._refresh_card(run)
            if run.state == PUBLISHING:
                await self._publish_plan(run)
                await transition(self.db, run, REPORTING)
                await self._refresh_card(run)
            if run.state == REPORTING:
                await self._report_planning(run)
                await transition(self.db, run, DONE)
                await self._refresh_card(run)
                await self.sb.delete_app(run.app_id)
        except RunFailure as f:
            await self.fail(run, f.stage, str(f))
        except Exception as e:  # noqa: BLE001 — every failure must still be reported
            await self.fail(run, run.state, f"internal error: {e!r}")
        finally:
            await self._emit_run_span(run)

    async def _planning(self, run: Run) -> PlannerResult:
        """Planner produces spec+plan; the Implementor Advisor gates them.

        Both agents are configurable per repository (`planning:` in `.loop.yml`,
        snapshotted onto the Run at prepare): each model falls back to its
        `LOOP_*` setting, and `planning.advisor.enabled: false` publishes the
        first plan the planner returns.
        """
        deadline = clock.monotonic() + run.timeout_minutes * 60
        task_timeout_s = min(run.timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        planner_model = run.planner_model or self.settings.planner_model or None
        advisor_model = run.advisor_model or self.settings.advisor_model
        max_iterations = (run.plan_max_iterations if run.plan_max_iterations is not None
                          else self.settings.plan_max_iterations)
        prompt = run.prompt
        iteration = 0
        while True:
            try:
                # Every round is a fresh session, revise included. sandboxd can
                # only resume *the most recent* session, and after round 0 that
                # is the advisor's — `continue` would have handed the planner
                # the reviewer's context, not its own. Resuming the planner is
                # not worth restoring either: an advisor round outlives the
                # five-minute prompt cache, so the inherited context would come
                # back at write price. The revise prompt carries what the
                # session used to (see build_planner_revise_prompt).
                task, deadline = await self._run_sandbox_task(
                    run, prompt, task_timeout_s, deadline,
                    model=planner_model,
                    continue_session=False,
                    trace_stage=f"plan-{iteration}")
                result = parse_planner_output(task.get("agent_message_final")
                                              or task.get("agent_message") or "")
            except ReviewDeadline:
                raise RunFailure(PLANNING, "planning timed out") from None
            except (ReviewTaskError, PlanningError) as e:
                raise RunFailure(PLANNING, f"planner failed: {e}") from e
            if result.outcome == "questions":
                return result
            if not run.advisor_enabled:
                # The repository publishes what its planner wrote. Recorded, so
                # a plan that turns out to be thin can be told apart from one an
                # advisor waved through.
                await dbmod.add_event(self.db, run.id, PLANNING, PLANNING,
                                      "advisor disabled by .loop.yml")
                return result
            try:
                # The advisor gets a fresh session too: it must judge the
                # written documents, not inherit the planner's reasoning. That
                # was the intent from day one, but until `continue` became
                # explicit the platform's default silently handed it the
                # planner's session — exactly what this call rules out.
                task, deadline = await self._run_sandbox_task(
                    run, build_advisor_prompt(run.spec_path, run.plan_path),
                    task_timeout_s, deadline, model=advisor_model,
                    continue_session=False, trace_stage=f"advisor-{iteration}")
                verdict = parse_advisor_verdict(task.get("agent_message_final")
                                                or task.get("agent_message") or "")
            except ReviewDeadline:
                raise RunFailure(PLANNING, "planning timed out") from None
            except (ReviewTaskError, PlanningError) as e:
                raise RunFailure(PLANNING, f"advisor failed: {e}") from e
            await dbmod.add_event(self.db, run.id, PLANNING, PLANNING,
                                  f"advisor verdict: {verdict.verdict}")
            if verdict.verdict == "approved":
                return result
            if iteration >= max_iterations:
                raise RunFailure(
                    PLANNING,
                    "the advisor did not approve the plan after "
                    f"{iteration + 1} iteration(s): {verdict.summary} "
                    f"(issues: {'; '.join(verdict.issues)})")
            iteration += 1
            prompt = build_planner_revise_prompt(verdict, run.issue_number,
                                                 run.spec_path, run.plan_path)
