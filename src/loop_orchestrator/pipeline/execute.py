"""The executing stage: the agent working the plan, and the run's time budget.

A separate polling loop from `_run_sandbox_task_inner` on purpose. This one
owns `run.task_id` (so a restart can pick the same task back up), reports its
overrun as `ExecutionTimeout` rather than `ReviewDeadline`, and resumes with a
prompt that names the plan. What the two share — how a failed task is read —
lives in `constants.py`.
"""

from .. import db as dbmod
from ..models import EXECUTING, Run
from . import clock
from .constants import MAX_TASK_TIMEOUT_S, failure_blob, is_rate_limited, is_transient
from .errors import ExecutionTimeout, RunFailure

EXECUTE_CONTINUE_PROMPT = "Continue executing the plan from where you stopped."


class ExecuteMixin:
    async def _execute(self, run: Run) -> None:
        timeout_s = min(run.timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        if not run.task_id:
            # First task in a brand-new sandbox, so there is nothing to inherit
            # — but say so explicitly rather than leaning on the platform's
            # "continue if a session exists" default.
            run.task_id = await self._submit_resumable(run, run.prompt, timeout_s,
                                                       continue_session=False)
            await dbmod.save_run(self.db, run)
        # The budget covers agent working time only; waiting out subscription limits
        # is not work, so every rate-limit pause pushes the deadline forward.
        deadline = clock.monotonic() + timeout_s
        rate_limit_attempts = 0
        transient_attempts = 0
        while True:
            if clock.monotonic() >= deadline:
                raise ExecutionTimeout
            task = await self._task_status(run, run.task_id)
            if task is None:
                await self._poll_wait(run)
                continue
            status = task.get("status")
            if status == "running":
                await self._poll_wait(run)
                continue
            if status == "succeeded":
                # GET /v1/sandboxes/{id}/tasks/{taskId} serialises TaskResult, whose
                # field is agent_message_final; agent_message is the list-endpoint name.
                run.summary = (task.get("agent_message_final")
                               or task.get("agent_message") or "(no summary)")
                await dbmod.save_run(self.db, run)
                return
            blob = failure_blob(task)
            if status == "failed" and rate_limit_attempts < 3 and is_rate_limited(blob):
                rate_limit_attempts += 1
                await self.tg.send(
                    f"⏳ Run #{run.id}: hit the subscription rate limit, resuming in "
                    f"{self.settings.rate_limit_retry_minutes} min "
                    f"(attempt {rate_limit_attempts}/3).",
                    thread_id=run.tg_thread_id)
                paused_at = clock.monotonic()
                await self._sleep_awake(run, self.settings.rate_limit_retry_minutes * 60)
                deadline += clock.monotonic() - paused_at
                run.task_id = await self.sb.submit_task(
                    run.sandbox_id, EXECUTE_CONTINUE_PROMPT,
                    timeout_s=timeout_s, continue_session=True)
                await dbmod.save_run(self.db, run)
                continue
            if (status == "failed"
                    and transient_attempts < self.settings.agent_retry_attempts
                    and is_transient(blob)):
                transient_attempts += 1
                await dbmod.add_event(
                    self.db, run.id, EXECUTING, EXECUTING,
                    f"transient agent error, resuming the session (attempt "
                    f"{transient_attempts}/{self.settings.agent_retry_attempts}): "
                    f"{task.get('error_message') or 'no details'}")
                # Stalls cluster during provider incidents; give the outage a
                # moment to pass instead of resuming straight into it.
                await self._sleep_awake(run, self.settings.agent_retry_backoff_seconds)
                run.task_id = await self.sb.submit_task(
                    run.sandbox_id, EXECUTE_CONTINUE_PROMPT,
                    timeout_s=timeout_s, continue_session=True)
                await dbmod.save_run(self.db, run)
                continue
            raise RunFailure(
                EXECUTING,
                f"task finished with status {status}: "
                f"{task.get('error_message') or 'no details'}")
