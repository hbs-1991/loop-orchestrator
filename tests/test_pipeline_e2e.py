"""The _e2e cycle: verdicts, fix iterations, escalation, skip, deadline."""
import json

from loop_orchestrator import db as dbmod
from loop_orchestrator.models import E2E_TESTING
from loop_orchestrator.pipeline import Pipeline

from tests.conftest import FakeGitHub, FakeSandboxd, FakeSettings, FakeTG

PASSED = json.dumps({"verdict": "passed", "summary": "works",
                     "tests": [{"title": "main", "status": "passed",
                                "video": ".loop/e2e/main.mp4"}],
                     "main_video": ".loop/e2e/main.mp4"})
FAILED_V = json.dumps({"verdict": "failed", "summary": "broken",
                       "tests": [{"title": "main", "status": "failed",
                                  "video": ".loop/e2e/fail-1.mp4"}],
                       "main_video": None})


def make_pipeline(db):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    return Pipeline(db, FakeSettings(), gh, sb, tg), gh, sb, tg


async def make_e2e_run(db, **overrides):
    run = await dbmod.create_run(db, "o/r", 1, "feat")
    run.state = E2E_TESTING
    run.sandbox_id = "sb-1"
    run.spec_path = "docs/specs/f-design.md"
    run.run_cmd = "npm run dev"
    run.e2e_enabled = True
    run.e2e_max_iterations = overrides.pop("e2e_max_iterations", 2)
    run.timeout_minutes = overrides.pop("timeout_minutes", 30)
    run.test_cmd = "npm test"
    for k, v in overrides.items():
        setattr(run, k, v)
    await dbmod.save_run(db, run)
    return run


async def test_e2e_passed_first_try(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_e2e_run(db)
    sb.task_results = [{"status": "succeeded", "agent_message_final": PASSED}]
    await p._e2e(run)
    assert run.e2e_status == "passed"
    assert run.e2e_iteration == 0
    report = json.loads(run.e2e_json)
    assert report["main_video"] == ".loop/e2e/main.mp4"
    assert len(sb.tasks_submitted) == 1
    assert sb.tasks_submitted[0]["model"] is None  # e2e_model="" -> executor default


async def test_e2e_fix_cycle_then_passed(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_e2e_run(db)
    sb.task_results = [
        {"status": "succeeded", "agent_message_final": FAILED_V},   # e2e run 1
        {"status": "succeeded", "agent_message_final": "fixed"},    # fix task
        {"status": "succeeded", "agent_message_final": PASSED},     # e2e run 2
    ]
    await p._e2e(run)
    assert run.e2e_status == "passed"
    assert run.e2e_iteration == 1
    assert len(sb.tasks_submitted) == 3
    assert "Do not weaken" in sb.tasks_submitted[1]["prompt"]


async def test_e2e_escalates_at_limit(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_e2e_run(db, e2e_max_iterations=0)
    sb.task_results = [{"status": "succeeded", "agent_message_final": FAILED_V}]
    await p._e2e(run)
    assert run.e2e_status == "escalated"
    assert json.loads(run.e2e_json)["tests"][0]["status"] == "failed"


async def test_e2e_skipped_after_retry(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_e2e_run(db)
    sb.task_results = [
        {"status": "failed", "error_message": "boom"},
        {"status": "failed", "error_message": "boom again"},
    ]
    await p._e2e(run)
    assert run.e2e_status == "skipped"
    assert "boom" in json.loads(run.e2e_json)["summary"]


async def test_e2e_unparsable_verdict_retries_then_skips(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_e2e_run(db)
    sb.task_results = [
        {"status": "succeeded", "agent_message_final": "not json"},
        {"status": "succeeded", "agent_message_final": "still not json"},
    ]
    await p._e2e(run)
    assert run.e2e_status == "skipped"


async def test_e2e_deadline_escalates(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_e2e_run(db, timeout_minutes=0)
    sb.task_results = [{"status": "running"}]
    await p._e2e(run)
    assert run.e2e_status == "escalated"
    assert "timeout" in json.loads(run.e2e_json)["summary"]
