"""The contracting stage: capture the interface for the tasks this issue blocks."""
import json

import httpx

from loop_orchestrator import db as dbmod
from loop_orchestrator.models import DONE

from tests.conftest import FakeGitHub, FakeSandboxd, FakeTG
from tests.test_pipeline_prepare import make_pipeline, seed_ok
from tests.test_pipeline_review import seed_run_env

# Review off and `approval: never` so exactly two agent tasks run and the Run
# reaches DONE without pausing: what these tests assert is the stage in
# between, and the review and the pause have their own files.
LOOP_YML = ("specs_dir: docs/superpowers/specs\ntest: npm test\n"
            "review:\n  enabled: false\n"
            "approval: never\n")

EXEC_OK = {"status": "succeeded", "agent_message_final": "did the work"}
CONTRACT_OK = {"status": "succeeded", "agent_message_final": json.dumps({
    "outcome": "contract", "contract": "### POST /v1/ingest",
    "sources": ["src/api/ingest.py"], "breaking_changes": []})}
CONTRACT_JUNK = {"status": "succeeded", "agent_message_final": "no json at all"}


async def start_run(db, gh, sb, tg, tmp_path, issue_number=12):
    seed_ok(gh, tmp_path)
    gh.files[".loop.yml"] = LOOP_YML
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    run.issue_number = issue_number
    await dbmod.save_run(db, run)
    seed_run_env(gh, sb, tmp_path, run.id)
    return make_pipeline(db, tmp_path, gh=gh, sb=sb, tg=tg), run


async def test_contract_captured_when_the_issue_blocks_another(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    gh.blocking[12] = [{"repo": "o/frontend", "number": 13, "state": "open"}]
    gh.branch_shas["feat/x"] = "headsha1"
    sb.task_results = [EXEC_OK, CONTRACT_OK]
    await pipe.process(run)
    assert run.state == DONE
    assert run.contract_status == "produced"
    task = sb.tasks_submitted[1]
    assert task["model"] == "claude-sonnet-5"
    assert task["continue"] is False          # describes the code, not the session
    assert "origin/feat/x..HEAD" in task["prompt"]
    row = await dbmod.get_contract(db, "o/myrepo", 12)
    assert row["contract_md"] == "### POST /v1/ingest"
    assert json.loads(row["sources_json"]) == ["src/api/ingest.py"]
    assert row["pr_number"] == 5 and row["head_sha"] == "headsha1"


async def test_stage_skipped_when_the_issue_blocks_nobody(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    sb.task_results = [EXEC_OK]               # no contract task is submitted
    await pipe.process(run)
    assert run.state == DONE and run.contract_status == "skipped"
    assert len(sb.tasks_submitted) == 1
    assert await dbmod.get_contract(db, "o/myrepo", 12) is None


async def test_run_without_an_issue_never_enters_the_stage(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    gh.files[".loop.yml"] = LOOP_YML
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    seed_run_env(gh, sb, tmp_path, run.id)
    pipe = make_pipeline(db, tmp_path, gh=gh, sb=sb, tg=tg)
    sb.task_results = [EXEC_OK]
    await pipe.process(run)
    assert run.state == DONE
    assert not run.contract_enabled and run.contract_status is None
    states = [e[0] for e in await dbmod.events_for_run(db, run.id)]
    assert "contracting" not in states


async def test_a_broken_verdict_does_not_block_publication(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    gh.blocking[12] = [{"repo": "o/frontend", "number": 13, "state": "open"}]
    sb.task_results = [EXEC_OK, CONTRACT_JUNK]
    await pipe.process(run)
    assert run.state == DONE                  # publication went through
    assert run.contract_status == "failed"
    assert await dbmod.get_contract(db, "o/myrepo", 12) is None


async def test_outcome_none_is_recorded_as_a_result(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    gh.blocking[12] = [{"repo": "o/frontend", "number": 13, "state": "open"}]
    sb.task_results = [EXEC_OK, {"status": "succeeded", "agent_message_final":
                                 '{"outcome": "none", "contract": "", "sources": []}'}]
    await pipe.process(run)
    assert run.contract_status == "none"
    row = await dbmod.get_contract(db, "o/myrepo", 12)
    assert row is not None and row["contract_md"] == ""


async def test_contract_is_published_as_a_marked_issue_comment(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    gh.blocking[12] = [{"repo": "o/frontend", "number": 13, "state": "open"}]
    sb.task_results = [EXEC_OK, CONTRACT_OK]
    await pipe.process(run)
    body = next(c for c in gh.comments if "<!-- loop:api-contract -->" in c)
    assert "### POST /v1/ingest" in body
    assert "`src/api/ingest.py`" in body


async def test_republishing_edits_the_existing_comment(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    gh.blocking[12] = [{"repo": "o/frontend", "number": 13, "state": "open"}]
    gh.issue_comments[12] = [{"id": 77, "body": "<!-- loop:api-contract -->old"}]
    sb.task_results = [EXEC_OK, CONTRACT_OK]
    await pipe.process(run)
    assert [cid for cid, _ in gh.comments_updated] == [77]
    assert not any("<!-- loop:api-contract -->" in c for c in gh.comments)


def die_on_the_contract_task(sb, failure):
    """Let the executor's task through, then break the next submit.

    The stage under test is the second submit, so the fake has to change its
    mind between the two rather than be broken from the start.
    """
    real_submit = sb.submit_task

    async def submit(sandbox_id, prompt, timeout_s, **kw):
        if sb.tasks_submitted:                # the executor has already run
            failure()
        return await real_submit(sandbox_id, prompt, timeout_s, **kw)

    sb.submit_task = submit


async def test_a_dead_sandbox_at_contracting_does_not_fail_the_run(db, tmp_path):
    """The spec names a dead sandbox as a contracting failure that proceeds."""
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    gh.blocking[12] = [{"repo": "o/frontend", "number": 13, "state": "open"}]
    sb.task_results = [EXEC_OK]               # the contract task never starts

    def die():
        sb.submit_conflicts = 99              # 409 forever, because...
        sb.sandbox_info = {"status": "error"}  # ...the sandbox will never be up
    die_on_the_contract_task(sb, die)

    await pipe.process(run)
    assert run.state == DONE                  # publication went through
    assert run.contract_status == "failed"
    details = [r[0] for r in await db.execute_fetchall(
        "SELECT detail FROM run_events WHERE run_id=? AND to_state='contracting'",
        (run.id,))]
    assert any("state 'error'" in d for d in details)


async def test_a_4xx_on_the_contract_task_does_not_fail_the_run(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    gh.blocking[12] = [{"repo": "o/frontend", "number": 13, "state": "open"}]
    sb.task_results = [EXEC_OK]

    def die():
        req = httpx.Request("POST", "http://sb/v1/sandboxes/sb-1/tasks")
        raise httpx.HTTPStatusError("422 Unprocessable", request=req,
                                    response=httpx.Response(422, request=req))
    die_on_the_contract_task(sb, die)

    await pipe.process(run)
    assert run.state == DONE
    assert run.contract_status == "failed"
    assert await dbmod.get_contract(db, "o/myrepo", 12) is None


async def test_a_failed_capture_publishes_no_comment(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    pipe, run = await start_run(db, gh, sb, tg, tmp_path)
    gh.blocking[12] = [{"repo": "o/frontend", "number": 13, "state": "open"}]
    sb.task_results = [EXEC_OK, CONTRACT_JUNK]
    await pipe.process(run)
    assert not any("<!-- loop:api-contract -->" in c for c in gh.comments)
