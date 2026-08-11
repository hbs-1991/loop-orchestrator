"""Running one agent task in a sandbox, and surviving everything around it.

The platform-facing half of the pipeline: submitting a task to a sandbox that
may be busy, asleep or dead, polling it through a control plane that is not
always reachable, and resuming the session when the agent dies of a rate limit
or a dropped stream. Every stage that runs an agent goes through
`_run_sandbox_task`; `_execute` is the one exception and lives next door
(see `execute.py`).
"""

import asyncio

import httpx

from .. import db as dbmod
from ..models import Run
from . import clock
from .constants import CONTINUE_PROMPT, failure_blob, is_rate_limited, is_transient
from .errors import ReviewDeadline, ReviewTaskError, RunFailure


class SandboxTasksMixin:
    async def _submit_resumable(self, run: Run, prompt: str, timeout_s: int,
                                model: str | None = None,
                                continue_session: bool | None = None) -> str:
        """Submit a stage task, tolerating a sandbox that cannot accept it yet.

        sandboxd answers 409 while the sandbox is occupied or not ready: after
        an orchestrator restart the pre-restart task may still run (one task
        at a time — wait it out), and a freshly created sandbox may still be
        seeding its workspace (the first import of a new repo outlives
        sandbox creation, seen live on run #24). Retry until the deadline.

        A sandbox in `error` is the exception: it answers 409 as well and will
        never leave that state, so waiting one out only burns the whole stage
        budget in silence. Run #57 sat on one for three hours (the sandbox
        image had been pruned off the host overnight, so its workspace seed
        failed at creation) — that failure is now immediate and named.
        """
        deadline = clock.monotonic() + timeout_s
        while True:
            try:
                return await self.sb.submit_task(run.sandbox_id, prompt,
                                                 timeout_s=timeout_s, model=model,
                                                 continue_session=continue_session)
            except httpx.HTTPStatusError as e:
                if clock.monotonic() >= deadline:
                    raise
                if e.response.status_code == 409:
                    if await self._sandbox_is_dead(run):
                        raise RunFailure(
                            run.state,
                            "the sandbox is in state 'error' and cannot accept "
                            "tasks — sandboxd could not bring it up (check the "
                            "control-plane log for 'create: aborting')") from e
                # Anything other than "busy" is only worth retrying if the
                # sandbox turned out to be asleep and we just woke it.
                elif not await self._ensure_awake(run):
                    raise
            await self._drain_stale_task(run, max(0.0, deadline - clock.monotonic()))
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _sandbox_is_dead(self, run: Run) -> bool:
        """True only when sandboxd states the sandbox is in `error`.

        Deliberately one-sided: a control plane that is briefly unreachable, or
        a version that does not report a status, must not be read as a dead
        sandbox — the caller would fail a run that is merely waiting.
        """
        try:
            info = await self.sb.get_sandbox(run.sandbox_id)
        except httpx.HTTPError:
            return False
        return info.get("status") == "error"

    async def _ensure_awake(self, run: Run) -> bool:
        """Restart the sandbox if the idle reaper stopped it. True if it did.

        The reaper stops containers, it does not delete them: the workspace and
        the agent's session survive, so a stopped sandbox is worth waking
        rather than declaring the run dead. Seen live on run #40, whose
        sandbox was reaped while the stage sat out a rate-limit pause.
        """
        try:
            info = await self.sb.get_sandbox(run.sandbox_id)
        except httpx.HTTPError:
            return False
        if info.get("status") in (None, "running"):
            return False
        return await self.sb.start_sandbox(run.sandbox_id)

    async def _poll_wait(self, run: Run) -> None:
        """Wait one poll interval while holding the sandbox awake.

        Every polling loop goes through here: sandboxd's idle reaper counts an
        agent task as inactivity (see SandboxdClient.keepalive), so a stage that
        outlasts the instance threshold dies unless something keeps refreshing.
        """
        await self.sb.keepalive(run.sandbox_id, self.settings.keepalive_minutes)
        await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _sleep_awake(self, run: Run, seconds: float) -> None:
        """Sleep out a pause while holding the sandbox awake.

        A rate-limit pause (an hour by default) outlives both the keepalive
        window and sandboxd's idle threshold, and nothing polls during it — a
        plain sleep hands the reaper a sandbox to stop and the resume then
        lands in a dead one. Refresh twice per window instead.
        """
        step = max(60.0, self.settings.keepalive_minutes * 60 / 2)
        remaining = seconds
        while remaining > 0:
            await self.sb.keepalive(run.sandbox_id, self.settings.keepalive_minutes)
            await asyncio.sleep(min(step, remaining))
            remaining -= step
        # A keepalive lost to a restart or an older sandboxd still leaves the
        # sandbox reapable; the resume that follows deserves a live one.
        await self._ensure_awake(run)

    async def _task_status(self, run: Run, task_id: str) -> dict | None:
        """Read a task, or None when the control plane is briefly unreachable.

        A poll tick is not a checkpoint: skipping one costs a poll interval,
        while raising kills a run whose agent is still working in its sandbox.
        Docker's embedded DNS starts timing out when the host is loaded (runs
        #41 and #42 both died on EAI_AGAIN while their planners carried on
        planning), and with_retries' few seconds are nowhere near that outage.
        The stage deadline, which every caller re-checks each turn, stays the
        bound on waiting.
        """
        try:
            return await self.sb.get_task(run.sandbox_id, task_id)
        except httpx.TransportError:
            return None
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                return None
            raise

    async def _drain_stale_task(self, run: Run, timeout_s: int) -> None:
        try:
            tasks = await self.sb.list_tasks(run.sandbox_id)
        except httpx.TransportError:
            return  # unreachable control plane: the caller retries anyway
        stale = next((t for t in tasks if t.get("status") == "running"), None)
        if stale is None:
            return
        deadline = clock.monotonic() + timeout_s
        while clock.monotonic() < deadline:
            task = await self._task_status(run, stale["id"])
            if task is not None and task.get("status") != "running":
                return
            await self._poll_wait(run)

    async def _run_sandbox_task(self, run: Run, prompt: str, timeout_s: int,
                                deadline: float, model: str | None = None,
                                continue_session: bool | None = None,
                                trace_stage: str = "") -> tuple[dict, float]:
        """`_run_sandbox_task_inner`, plus the span for the session it just ran.

        The wrapper exists so the trace is emitted on the failure paths too: a
        stage that died halfway is exactly the one worth looking at, and its
        session file is on disk either way.
        """
        start_ns = self._trace_start()
        fresh = None if continue_session is None else not continue_session
        try:
            result = await self._run_sandbox_task_inner(
                run, prompt, timeout_s, deadline, model=model,
                continue_session=continue_session)
        except Exception as e:  # noqa: BLE001 — re-raised immediately
            if trace_stage:
                await self._trace_task(run, trace_stage, fresh=fresh,
                                       model=model or "", start_ns=start_ns,
                                       status="error", error=repr(e))
            raise
        if trace_stage:
            await self._trace_task(run, trace_stage, fresh=fresh,
                                   model=model or "", start_ns=start_ns)
        return result

    async def _run_sandbox_task_inner(self, run: Run, prompt: str, timeout_s: int,
                                      deadline: float, model: str | None = None,
                                      continue_session: bool | None = None) -> tuple[dict, float]:
        """Submit a task and poll it to completion within the given deadline.

        Subscription rate-limit pauses extend the deadline (waiting is not work).
        Returns (final task dict, possibly-extended deadline).

        `continue_session` is the tri-state of SandboxdClient.submit_task and
        every caller passes it explicitly: leaving it to the platform means
        "resume the previous stage's session", which is almost never what a new
        stage wants. The resumes *inside* this method are the exception — they
        continue this stage's own interrupted session, so they force True.
        """
        task_id = await self._submit_resumable(run, prompt, timeout_s, model=model,
                                               continue_session=continue_session)
        rate_limit_attempts = 0
        transient_attempts = 0
        while True:
            if clock.monotonic() >= deadline:
                await self.sb.cancel_task(run.sandbox_id, task_id)
                raise ReviewDeadline
            task = await self._task_status(run, task_id)
            if task is None:
                await self._poll_wait(run)
                continue
            status = task.get("status")
            if status == "running":
                await self._poll_wait(run)
                continue
            if status == "succeeded":
                return task, deadline
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
                task_id = await self.sb.submit_task(
                    run.sandbox_id, CONTINUE_PROMPT,
                    timeout_s=timeout_s, continue_session=True, model=model)
                continue
            if (status == "failed"
                    and transient_attempts < self.settings.agent_retry_attempts
                    and is_transient(blob)):
                transient_attempts += 1
                await dbmod.add_event(
                    self.db, run.id, run.state, run.state,
                    f"transient agent error, resuming the session (attempt "
                    f"{transient_attempts}/{self.settings.agent_retry_attempts}): "
                    f"{task.get('error_message') or 'no details'}")
                # Same backoff as _execute: don't resume into the same outage.
                await self._sleep_awake(run, self.settings.agent_retry_backoff_seconds)
                task_id = await self.sb.submit_task(
                    run.sandbox_id, CONTINUE_PROMPT,
                    timeout_s=timeout_s, continue_session=True, model=model)
                continue
            raise ReviewTaskError(
                f"task finished with status {status}: "
                f"{task.get('error_message') or 'no details'}")
