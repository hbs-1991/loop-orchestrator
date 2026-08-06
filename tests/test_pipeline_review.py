"""Integration tests for the reviewing step (review + fix cycle)."""
import json

from loop_orchestrator import db as dbmod
from loop_orchestrator.models import DONE, REVIEWING

from tests.conftest import FakeGitHub, FakeSandboxd, FakeTG
from tests.test_pipeline_prepare import make_pipeline, seed_ok

CLEAN = {"status": "succeeded",
         "agent_message_final": '{"verdict": "clean", "summary": "ok", "findings": []}'}
FINDINGS = {"status": "succeeded",
            "agent_message_final": json.dumps({
                "verdict": "findings", "summary": "issues",
                "findings": [{"severity": "major", "file": "a.py",
                              "title": "bug A", "detail": "d"}]})}
EXEC_OK = {"status": "succeeded", "agent_message_final": "did the work"}
FIX_OK = {"status": "succeeded", "agent_message_final": "fixed"}


def seed_run_env(gh, sb, tmp_path, run_id):
    branch = f"loop/run-{run_id}"
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 2}
    gh.branch_shas[branch] = "sha1"


async def start_run(db, gh, sb, tg, tmp_path):
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    seed_run_env(gh, sb, tmp_path, run.id)
    pipe = make_pipeline(db, tmp_path, gh=gh, sb=sb, tg=tg)
    return pipe, run


async def test_clean_on_first_pass(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    sb.task_results = [EXEC_OK, CLEAN]
    await pipe.process(run)
    assert run.state == DONE
    assert run.review_status == "clean" and run.review_iteration == 0
    # Review task pinned to the reviewer model, fresh session.
    review_task = sb.tasks_submitted[1]
    assert review_task["model"] == "claude-fable-5"
    assert review_task["continue"] is False
    assert ["loop:done"] in gh.labels_added
    assert any("review (Fable 5)" in c and "✅ clean" in c for c in gh.comments)


async def test_findings_then_fix_then_clean(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    sb.task_results = [EXEC_OK, FINDINGS, FIX_OK, CLEAN]
    await pipe.process(run)
    assert run.state == DONE
    assert run.review_status == "clean" and run.review_iteration == 1
    fix_task = sb.tasks_submitted[2]
    assert fix_task["model"] is None  # fix runs on the agent's default model
    assert "bug A" in fix_task["prompt"] and "npm test" in fix_task["prompt"]
    report = json.loads(run.review_json)
    assert [f["title"] for f in report["fixed"]] == ["bug A"]
    assert report["remaining"] == []
    assert any("Fixed in the fix cycle (1)" in c for c in gh.comments)


async def test_iterations_exhausted_escalates(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    # max_fix_iterations: 0 via .loop.yml → findings escalate immediately.
    gh.files[".loop.yml"] += "review:\n  max_fix_iterations: 0\n"
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    seed_run_env(gh, sb, tmp_path, run.id)
    pipe = make_pipeline(db, tmp_path, gh=gh, sb=sb, tg=tg)
    sb.task_results = [EXEC_OK, FINDINGS]
    await pipe.process(run)
    assert run.state == DONE  # code is still published
    assert run.review_status == "escalated"
    assert ["loop:needs-review"] in gh.labels_added
    assert ["loop:done"] not in gh.labels_added
    assert f"escalation:{run.id}:1" in tg.sent
    assert any("⚠️ findings remain" in c for c in gh.comments)


async def test_reviewer_fails_twice_review_skipped(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    sb.task_results = [EXEC_OK,
                       {"status": "failed", "error_message": "agent crashed"},
                       {"status": "failed", "error_message": "agent crashed again"}]
    await pipe.process(run)
    assert run.state == DONE
    assert run.review_status == "skipped"
    assert ["loop:done"] in gh.labels_added  # skipped review does not block delivery
    assert any("⛔ review skipped" in c for c in gh.comments)


async def test_invalid_verdict_retries_then_succeeds(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    sb.task_results = [EXEC_OK,
                       {"status": "succeeded", "agent_message_final": "not json at all"},
                       CLEAN]
    await pipe.process(run)
    assert run.state == DONE
    assert run.review_status == "clean"


async def test_review_disabled_skips_reviewing(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    gh.files[".loop.yml"] += "review:\n  enabled: false\n"
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    seed_run_env(gh, sb, tmp_path, run.id)
    pipe = make_pipeline(db, tmp_path, gh=gh, sb=sb, tg=tg)
    sb.task_results = [EXEC_OK]
    await pipe.process(run)
    assert run.state == DONE
    assert run.review_status is None
    assert len(sb.tasks_submitted) == 1  # executor only, no review task
    async with db.execute(
            "SELECT to_state FROM run_events WHERE run_id = ?", (run.id,)) as cur:
        to_states = [r["to_state"] for r in await cur.fetchall()]
    assert REVIEWING not in to_states


async def test_rate_limit_inside_review_resumes(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    pipe.settings.rate_limit_retry_minutes = 0  # no real sleep in tests
    sb.task_results = [EXEC_OK,
                       {"status": "failed", "error_message": "usage limit reached"},
                       CLEAN]
    await pipe.process(run)
    assert run.state == DONE
    assert run.review_status == "clean"
    resumed = sb.tasks_submitted[2]
    assert resumed["continue"] is True and resumed["model"] == "claude-fable-5"
