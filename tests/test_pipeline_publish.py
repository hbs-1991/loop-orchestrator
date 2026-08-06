import pytest

from loop_orchestrator import db as dbmod
from loop_orchestrator.clients.github import FastForwardError
from loop_orchestrator.models import AWAITING_APPROVAL, Run
from loop_orchestrator.pipeline import Pipeline, RunFailure

from tests.conftest import FakeGitHub, FakeSandboxd, FakeSettings, FakeTG


def make_pipe(db, gh, sb):
    return Pipeline(db=db, settings=FakeSettings(), gh=gh, sb=sb, tg=FakeTG())


async def pub_run(db) -> Run:
    run = await dbmod.create_run(db, "o/r", 5, "feat/x")
    run.app_id = "app-1"
    await dbmod.save_run(db, run)
    return run


async def test_publish_happy_path(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    run = await pub_run(db)
    branch = f"loop/run-{run.id}"
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 3}
    gh.branch_shas[branch] = "sha42"
    pipe = make_pipe(db, gh, sb)
    assert await pipe._stage(run) is True
    assert run.staging_branch == branch
    await pipe._publish_ff(run)
    assert gh.ff_calls == [("feat/x", "sha42")]
    assert gh.deleted_branches == [branch]


async def test_publish_no_commits_is_soft(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    sb.push_resp = {"pushed": False, "reason": "no_local_commits"}
    run = await pub_run(db)
    pipe = make_pipe(db, gh, sb)
    assert await pipe._stage(run) is False
    assert "made no code changes" in (run.summary or "")
    await pipe._publish_ff(run)            # nothing staged — a no-op
    assert gh.ff_calls == []


async def test_publish_push_refused(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    sb.push_resp = {"pushed": False, "reason": "unsafe_repo_config"}
    run = await pub_run(db)
    with pytest.raises(RunFailure) as e:
        await make_pipe(db, gh, sb)._stage(run)
    assert "unsafe_repo_config" in str(e.value)


async def test_publish_non_fast_forward_keeps_branch(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    run = await pub_run(db)
    branch = f"loop/run-{run.id}"
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 1}
    gh.branch_shas[branch] = "sha42"
    gh.ff_error = FastForwardError("not a fast forward")
    pipe = make_pipe(db, gh, sb)
    await pipe._stage(run)
    with pytest.raises(RunFailure) as e:
        await pipe._publish_ff(run)
    assert branch in str(e.value)          # a hint at where to find the code
    assert gh.deleted_branches == []       # the branch was kept


async def test_publish_partial_swallows(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    sb.push_resp = {"pushed": False, "reason": "push_failed"}
    run = await pub_run(db)
    await make_pipe(db, gh, sb)._publish_partial(run)  # does not raise


async def test_rescue_to_staging_swallows_errors(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    sb.push_resp = {"pushed": False, "reason": "push_failed"}
    run = await pub_run(db)
    assert await make_pipe(db, gh, sb).rescue_to_staging(run) is False
    assert gh.ff_calls == []               # the PR branch is never touched


async def test_rescue_to_staging_pushes(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    run = await pub_run(db)
    branch = f"loop/run-{run.id}"
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 1}
    assert await make_pipe(db, gh, sb).rescue_to_staging(run) is True
    assert run.staging_branch == branch
    assert gh.ff_calls == []


async def test_expire_preview_deletes_sandbox_and_keeps_run(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    run = await pub_run(db)
    run.state = AWAITING_APPROVAL
    run.sandbox_id = "sb-app-1"
    run.preview_url = "https://s-x-3000.preview.test"
    run.sandbox_expires_at = "2026-08-03 10:00:00"
    await dbmod.save_run(db, run)
    await make_pipe(db, gh, sb).expire_preview(run)
    assert sb.apps_deleted == ["app-1"]
    fresh = await dbmod.get_run(db, run.id)
    assert fresh.state == AWAITING_APPROVAL
    assert fresh.app_id is None and fresh.preview_url is None
