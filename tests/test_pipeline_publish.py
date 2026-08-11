import pytest

from loop_orchestrator import db as dbmod
from loop_orchestrator.clients.github import FastForwardError
from loop_orchestrator.models import AWAITING_APPROVAL, Run
from loop_orchestrator.pipeline import (
    Pipeline,
    RunFailure,
    build_preview_manifest,
    build_preview_script,
    build_sync_prompt,
    manifest_guard_script,
)

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


def test_sync_prompt_covers_the_hazards_git_merges_silently():
    p = build_sync_prompt("o/r", "main")
    # The whole point of the step: a clean merge is not a safe merge when both
    # sides added "the next" numbered artefact under a different filename.
    assert "sequentially numbered" in p and "without a conflict" in p.lower()
    assert "one head" in p                      # forked migration graph
    assert "renumber YOUR side" in p
    # Generated and scratch files are not resolved by reading them.
    assert "lockfiles" in p and "re-derive" in p
    assert ".loop/" in p


def test_sync_prompt_overrides_a_repo_skill_that_wants_a_pull_request():
    """Target repos ship `create-pr`-style skills whose last step is push + `gh
    pr create`. The resolver is the one loop agent whose task matches such a
    skill's triggers, and it can do neither: push is a control-plane operation
    and the image has no `gh`. Say so explicitly rather than let it try."""
    p = build_sync_prompt("o/r", "main")
    assert "Do not push" in p
    assert "do not run `gh`" in p
    assert "does not apply here" in p


def test_preview_script_sources_secrets_and_exports_env():
    script = build_preview_script("npm run dev", 3000, {"API_URL": "http://x y"})
    assert script.startswith("cd /home/sandbox/workspace/app || exit 1")
    # Secret values never enter the command line — the sandbox already has them
    # in a file, and the script only sources it.
    assert "[ -f .loop/secrets.env ] && . .loop/secrets.env" in script
    assert "export API_URL='http://x y'" in script          # env values are quoted
    assert "nohup npm run dev > .loop/preview.log 2>&1 &" in script
    # A retried exec must not start a second server on top of a live one.
    assert script.index("python3") < script.index("nohup")


async def test_preview_url_is_withheld_when_the_port_never_answers(db, monkeypatch):
    monkeypatch.setattr("loop_orchestrator.pipeline.preview.PREVIEW_READY_TIMEOUT_S", 0)
    gh, sb = FakeGitHub(), FakeSandboxd()
    run = await pub_run(db)
    run.sandbox_id, run.run_cmd = "sb-1", "npm run dev"
    sb.exec_results = [{"stdout": "", "stderr": "", "exit_code": 0},      # start
                       {"stdout": "EADDRINUSE :3000", "stderr": "", "exit_code": 0}]
    await make_pipe(db, gh, sb)._start_preview(run)
    # No dead link in the approval card...
    assert run.preview_url is None
    # ...but the reason survives the sandbox it came from.
    events = await dbmod.events_for_run(db, run.id)
    assert events, "the failure should leave a trace on the run"
    detail = (await db.execute_fetchall(
        "SELECT detail FROM run_events WHERE run_id = ?", (run.id,)))[-1][0]
    assert "EADDRINUSE" in detail


def test_preview_manifest_declares_the_same_server_to_the_platform():
    raw = build_preview_manifest("npm run dev", 3000, {"API_URL": "http://x y"})
    assert raw.startswith("version: 1\n")
    assert "port: 3000" in raw            # mandatory whenever command is set
    assert "health_path: /" in raw
    # Same contract as the exec script: secrets come from the file, never from
    # the command line, and env values are quoted.
    assert "[ -f .loop/secrets.env ] && . .loop/secrets.env" in raw
    assert "export API_URL='http://x y'" in raw
    assert "npm run dev" in raw


def test_preview_manifest_quoting_survives_a_command_with_quotes():
    raw = build_preview_manifest('sh -c "npm start"', 8080)
    # A hand-rolled quote would produce invalid YAML here; the value must round-trip.
    import yaml
    parsed = yaml.safe_load(raw)
    assert parsed["web"]["port"] == 8080
    assert parsed["web"]["command"].endswith('sh -c "npm start"')


def test_manifest_guard_refuses_a_tracked_manifest_and_excludes_ours():
    script = manifest_guard_script()
    # Exit non-zero when the repo owns the file: overwriting it would ride into
    # the revise commit and the PR diff.
    assert "git ls-files --error-unmatch sandbox.yaml" in script and "exit 3" in script
    # .git/info/exclude is per-clone and never committed, so `git add -A` by the
    # revise agent cannot pick our file up.
    assert ".git/info/exclude" in script


async def test_arm_preview_manifest_declines_when_the_repo_tracks_one(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    run = await pub_run(db)
    run.sandbox_id, run.run_cmd = "sb-1", "npm run dev"
    sb.exec_results = [{"stdout": "", "stderr": "", "exit_code": 3}]   # tracked
    assert await make_pipe(db, gh, sb)._arm_preview_manifest(run, 3000, {}) is False
    assert not any(p == "sandbox.yaml" for _, p, _ in sb.files_written)
    detail = (await db.execute_fetchall(
        "SELECT detail FROM run_events WHERE run_id = ?", (run.id,)))[-1][0]
    assert "tracks its own sandbox.yaml" in detail


async def test_arm_preview_manifest_declines_when_the_platform_rejects_it(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    run = await pub_run(db)
    run.sandbox_id, run.run_cmd = "sb-1", "npm run dev"
    sb.manifest_errors = ["web.command is set but web.port is missing"]
    assert await make_pipe(db, gh, sb)._arm_preview_manifest(run, 3000, {}) is False
    assert not any(p == "sandbox.yaml" for _, p, _ in sb.files_written)
    # A rejected manifest means no web process at all, so the pause must not
    # sleep on the strength of it.
    detail = (await db.execute_fetchall(
        "SELECT detail FROM run_events WHERE run_id = ?", (run.id,)))[-1][0]
    assert "manifest rejected" in detail


async def test_sleep_pause_records_a_refusal(db):
    gh, sb = FakeGitHub(), FakeSandboxd()
    run = await pub_run(db)
    run.state, run.sandbox_id = AWAITING_APPROVAL, "sb-1"
    sb.stop_ok = False                      # a task slipped in; sandboxd says 409
    await make_pipe(db, gh, sb)._sleep_pause(run)
    assert sb.stopped == []
    detail = (await db.execute_fetchall(
        "SELECT detail FROM run_events WHERE run_id = ?", (run.id,)))[-1][0]
    assert "could not sleep" in detail


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
