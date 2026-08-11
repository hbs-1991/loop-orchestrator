import asyncio

import httpx
import pytest

from loop_orchestrator import db as dbmod
from loop_orchestrator import pipeline as pipeline_mod
from loop_orchestrator.models import Run
from loop_orchestrator.pipeline import ExecutionTimeout, Pipeline, RunFailure
from loop_orchestrator.pipeline import clock as clock_mod

from tests.conftest import FakeGitHub, FakeSandboxd, FakeSettings, FakeTG


class FakeClock:
    """Monotonic clock advanced by the patched asyncio.sleep instead of real time."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def patch_clock(monkeypatch) -> FakeClock:
    clock = FakeClock()
    # One patch point for every stage: the stage modules call
    # `clock.monotonic()` through the module rather than binding the name.
    monkeypatch.setattr(clock_mod, "monotonic", clock)

    async def fake_sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return clock


def make_pipe(db, sb):
    return Pipeline(db=db, settings=FakeSettings(), gh=FakeGitHub(), sb=sb, tg=FakeTG())


async def executing_run(db) -> Run:
    run = await dbmod.create_run(db, "o/r", 5, "b")
    run.sandbox_id, run.prompt, run.timeout_minutes = "sb1", "do it", 90
    await dbmod.save_run(db, run)
    return run


async def test_execute_success(db):
    sb = FakeSandboxd()
    sb.task_results = [
        {"status": "running"},
        {"status": "succeeded", "agent_message_final": "all done"},
    ]
    run = await executing_run(db)
    await make_pipe(db, sb)._execute(run)
    assert run.summary == "all done"
    assert run.task_id == "task-1"
    assert sb.tasks_submitted[0]["timeout_s"] == 90 * 60
    # Explicitly fresh: sandboxd's `continue` default is "resume the previous
    # session", so an omitted field is not a clean start.
    assert sb.tasks_submitted[0]["continue"] is False


async def test_execute_failure_raises(db):
    sb = FakeSandboxd()
    sb.task_results = [{"status": "failed", "error_message": "agent exploded"}]
    run = await executing_run(db)
    with pytest.raises(RunFailure) as e:
        await make_pipe(db, sb)._execute(run)
    assert "agent exploded" in str(e.value)


async def test_execute_resumes_existing_task(db):
    sb = FakeSandboxd()
    sb.task_results = [{"status": "succeeded", "agent_message": "ok"}]
    run = await executing_run(db)
    run.task_id = "task-preexisting"
    await make_pipe(db, sb)._execute(run)
    assert sb.tasks_submitted == []  # did not resubmit the task


async def test_execute_rate_limit_retry(db, monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    sb = FakeSandboxd()
    sb.task_results = [
        {"status": "failed", "error_message": "Claude usage limit reached"},
        {"status": "succeeded", "agent_message": "finished up"},
    ]
    run = await executing_run(db)
    await make_pipe(db, sb)._execute(run)
    assert run.summary == "finished up"
    assert len(sb.tasks_submitted) == 2
    assert sb.tasks_submitted[1]["continue"] is True
    # The hour is slept in keepalive-sized slices, not in one go.
    assert sum(sleeps) == 60 * 60


async def test_execute_transient_api_error_resumes_the_session(db):
    # Run #32 died to a single "Response stalled mid-stream" 13 minutes into
    # planning; the session survives in the sandbox, so resume — don't fail.
    sb = FakeSandboxd()
    sb.task_results = [
        {"status": "failed", "error_message":
         "agent error: API Error: Response stalled mid-stream. "
         "The response above may be incomplete."},
        {"status": "succeeded", "agent_message_final": "done"},
    ]
    run = await executing_run(db)
    await make_pipe(db, sb)._execute(run)
    assert run.summary == "done"
    assert len(sb.tasks_submitted) == 2
    assert sb.tasks_submitted[1]["continue"] is True


async def test_execute_transient_retries_are_bounded(db):
    stall = {"status": "failed", "error_message":
             "agent error: API Error: Response stalled mid-stream."}
    sb = FakeSandboxd()
    sb.task_results = [stall, stall, stall]  # agent_retry_attempts = 2
    run = await executing_run(db)
    with pytest.raises(RunFailure, match="stalled mid-stream"):
        await make_pipe(db, sb)._execute(run)
    assert len(sb.tasks_submitted) == 3  # original + 2 resumes, then give up


async def test_run_sandbox_task_resumes_after_transient_api_error(db):
    # The same guard covers planner/advisor/review/e2e tasks.
    sb = FakeSandboxd()
    sb.task_results = [
        {"status": "failed", "error_message":
         "agent error: API Error: Response stalled mid-stream."},
        {"status": "succeeded", "agent_message_final": "verdict"},
    ]
    run = await executing_run(db)
    task, _ = await make_pipe(db, sb)._run_sandbox_task(
        run, "review please", 600, clock_mod.monotonic() + 600)
    assert task["agent_message_final"] == "verdict"
    assert len(sb.tasks_submitted) == 2
    assert sb.tasks_submitted[1]["continue"] is True


async def test_a_fresh_stage_still_forces_continue_on_recovery(db):
    # Review/e2e now start clean, but a stream drop must resume THAT task's
    # session: restarting it fresh would throw the stage's own work away.
    sb = FakeSandboxd()
    sb.task_results = [
        {"status": "failed", "error_message":
         "agent error: API Error: Response stalled mid-stream."},
        {"status": "succeeded", "agent_message_final": "verdict"},
    ]
    run = await executing_run(db)
    await make_pipe(db, sb)._run_sandbox_task(
        run, "review please", 600, clock_mod.monotonic() + 600,
        continue_session=False)
    assert [t["continue"] for t in sb.tasks_submitted] == [False, True]
    assert sb.tasks_submitted[1]["prompt"] == pipeline_mod.CONTINUE_PROMPT


async def test_execute_times_out_on_working_time(db, monkeypatch):
    patch_clock(monkeypatch)
    sb = FakeSandboxd()
    sb.task_results = [{"status": "running"}] * 100
    run = await executing_run(db)  # timeout_minutes = 90
    pipe = make_pipe(db, sb)
    pipe.settings.poll_interval_seconds = 600  # 10 minutes per poll
    with pytest.raises(ExecutionTimeout):
        await pipe._execute(run)
    assert len(sb.task_results) == 91  # 9 polls x 10 minutes = a 90-minute budget


async def test_execute_rate_limit_pause_is_outside_timeout(db, monkeypatch):
    clock = patch_clock(monkeypatch)
    sb = FakeSandboxd()
    sb.task_results = [
        {"status": "failed", "error_message": "Claude usage limit reached"},
        *[{"status": "running"}] * 5,
        {"status": "succeeded", "agent_message": "finished up"},
    ]
    run = await executing_run(db)  # timeout_minutes = 90
    pipe = make_pipe(db, sb)
    pipe.settings.poll_interval_seconds = 600
    await pipe._execute(run)
    assert run.summary == "finished up"
    # a 60-min pause + 50 min of polling: over the timeout by the clock, under it by work
    assert clock.now > 90 * 60


async def test_execute_survives_an_unreachable_control_plane(db):
    # Runs #41 and #42 died on a single EAI_AGAIN — docker's embedded DNS
    # times out when the host is loaded — while their agents kept planning.
    # A lost poll costs one interval; failing the run throws away the work.
    sb = FakeSandboxd()
    sb.task_results = [
        httpx.ConnectError("[Errno -3] Temporary failure in name resolution"),
        {"status": "running"},
        httpx.ReadTimeout("timed out"),
        {"status": "succeeded", "agent_message_final": "done"},
    ]
    run = await executing_run(db)
    await make_pipe(db, sb)._execute(run)
    assert run.summary == "done"
    assert len(sb.tasks_submitted) == 1  # the original task, never resubmitted


async def test_execute_still_fails_on_a_client_error(db):
    # 4xx is the control plane answering, not a network hiccup: a task that is
    # gone will never come back, so waiting out the deadline helps no one.
    sb = FakeSandboxd()
    req = httpx.Request("GET", "http://sb/v1/sandboxes/sb1/tasks/t1")
    sb.task_results = [httpx.HTTPStatusError(
        "404", request=req, response=httpx.Response(404, request=req))]
    run = await executing_run(db)
    with pytest.raises(httpx.HTTPStatusError):
        await make_pipe(db, sb)._execute(run)


async def test_rate_limit_pause_holds_the_sandbox_awake(db, monkeypatch):
    # Run #40's sandbox was reaped mid-pause: the hour outlasts both the
    # keepalive window and sandboxd's 35-minute idle threshold, and nothing
    # polls while we wait.
    patch_clock(monkeypatch)
    sb = FakeSandboxd()
    sb.task_results = [
        {"status": "failed", "error_message": "Claude usage limit reached"},
        {"status": "succeeded", "agent_message": "done"},
    ]
    run = await executing_run(db)
    await make_pipe(db, sb)._execute(run)
    # 60 min pause / half a 30 min keepalive window = 4 refreshes.
    assert sb.keepalives.count(("sb1", 30)) == 4


async def test_a_reaped_sandbox_is_restarted_before_the_resume(db, monkeypatch):
    patch_clock(monkeypatch)
    sb = FakeSandboxd()
    sb.sandbox_info = {"status": "stopped"}
    sb.task_results = [
        {"status": "failed", "error_message": "Claude usage limit reached"},
        {"status": "succeeded", "agent_message": "done"},
    ]
    run = await executing_run(db)
    await make_pipe(db, sb)._execute(run)
    assert sb.started == ["sb1"]
    assert run.summary == "done"


async def test_execute_keeps_the_sandbox_awake_while_polling(db):
    # sandboxd's idle reaper counts an agent task as inactivity, so a stage
    # longer than the instance threshold dies unless every poll refreshes.
    sb = FakeSandboxd()
    sb.task_results = [
        {"status": "running"},
        {"status": "running"},
        {"status": "succeeded", "agent_message_final": "done"},
    ]
    run = await executing_run(db)
    await make_pipe(db, sb)._execute(run)
    assert sb.keepalives == [("sb1", FakeSettings().keepalive_minutes)] * 2
