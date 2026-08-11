import io
import json
import zipfile

from loop_orchestrator import db as dbmod
from loop_orchestrator.models import (
    AWAITING_APPROVAL,
    CANCELLED,
    DONE,
    EXECUTING,
    FAILED,
    PREPARING,
    PUBLISHING,
    QUEUED,
    REPORTING,
    STAGING,
)
from loop_orchestrator.pipeline import Pipeline

from tests.conftest import FakeGitHub, FakeSandboxd, FakeSettings, FakeTG
from tests.test_pipeline_execute import patch_clock
from tests.test_pipeline_prepare import seed_ok


def make_pipe(db, tmp_path, gh, sb, tg):
    settings = FakeSettings()
    settings.secrets_dir = str(tmp_path / "secrets")
    return Pipeline(db=db, settings=settings, gh=gh, sb=sb, tg=tg)


def make_pipeline(db):
    """Pipeline with fresh fakes, for steps that need no repo secrets."""
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    return Pipeline(db=db, settings=FakeSettings(), gh=gh, sb=sb, tg=tg), gh, sb, tg


async def make_run_in(db, state):
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    run.state = state
    run.app_id = "app-1"
    run.sandbox_id = "sb-app-1"
    await dbmod.save_run(db, run)
    return run


async def test_process_full_success(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    branch = f"loop/run-{run.id}"
    sb.task_results = [
        {"status": "succeeded", "agent_message": "did the work"},
        {"status": "succeeded",
         "agent_message_final": '{"verdict": "clean", "summary": "ok", "findings": []}'},
    ]
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 2}
    gh.branch_shas[branch] = "sha1"
    await make_pipe(db, tmp_path, gh, sb, tg).process(run)
    assert run.state == DONE
    assert (await dbmod.get_run(db, run.id)).state == DONE
    assert "loop:run" in gh.labels_removed and "loop:running" in gh.labels_removed
    assert ["loop:running"] in gh.labels_added and ["loop:done"] in gh.labels_added
    assert any("did the work" in c for c in gh.comments)
    assert tg.sent == [f"done:{run.id}", f"thread-finished:{run.id}:{DONE}"]
    assert sb.apps_deleted == ["app-1"]  # cleanup after done


async def test_staging_clears_the_git_config_that_blocks_a_push(db, tmp_path):
    # Run #45 finished an advisor-approved plan and lost it at the push:
    # husky's core.hooksPath, written by `pnpm install`, makes sandboxd's
    # pre-push audit answer unsafe_repo_config.
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    run.sandbox_id = "sb-app-1"
    branch = f"loop/run-{run.id}"
    sb.unsafe_git_keys = ["core.hooksPath"]
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 1}
    assert await make_pipe(db, tmp_path, gh, sb, tg)._stage(run) is True
    assert sb.sanitized == ["sb-app-1"]
    async with db.execute(
            "SELECT detail FROM run_events WHERE run_id=?", (run.id,)) as cur:
        details = [r["detail"] for r in await cur.fetchall()]
    assert any("core.hooksPath" in d for d in details)


async def test_a_rerun_clears_the_previous_verdict_labels(db, tmp_path):
    # PR #16 was re-run to fix a test its first pass had missed, and ended up
    # wearing loop:run and loop:done at once — the old verdict never came off.
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    branch = f"loop/run-{run.id}"
    sb.task_results = [
        {"status": "succeeded", "agent_message": "fixed it"},
        {"status": "succeeded",
         "agent_message_final": '{"verdict": "clean", "summary": "ok", "findings": []}'},
    ]
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 1}
    gh.branch_shas[branch] = "sha1"
    await make_pipe(db, tmp_path, gh, sb, tg).process(run)
    for stale in ("loop:done", "loop:needs-review", "loop:failed"):
        assert stale in gh.labels_removed
    # …and the run's own verdict is the only one left standing at the end.
    assert gh.labels_added[-1] == ["loop:done"]
    assert gh.labels_removed.count("loop:failed") == 2  # cleared at start and at the verdict


async def test_process_prepare_failure_reports(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()  # no .loop.yml
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    pipe = make_pipe(db, tmp_path, gh, sb, tg)
    await pipe.process(run)
    assert run.state == FAILED
    assert ".loop.yml" in (run.error or "")
    assert ["loop:failed"] in gh.labels_added
    assert "loop:done" in gh.labels_removed  # a failure clears an earlier verdict too
    assert f"failed:{run.id}" in tg.sent
    assert sb.apps_deleted == []  # no app was created, so there is nothing to delete


async def test_process_execute_failure_publishes_partial(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    branch = f"loop/run-{run.id}"
    sb.task_results = [{"status": "failed", "error_message": "boom"}]
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 1}
    gh.branch_shas[branch] = "sha1"
    await make_pipe(db, tmp_path, gh, sb, tg).process(run)
    assert run.state == FAILED
    assert gh.ff_calls == [("feat/x", "sha1")]  # partial progress published
    assert f"failed:{run.id}" in tg.sent


async def test_process_execute_timeout_cancels_and_publishes_partial(db, tmp_path, monkeypatch):
    patch_clock(monkeypatch)
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    branch = f"loop/run-{run.id}"
    sb.task_results = [{"status": "running"}] * 100
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 1}
    gh.branch_shas[branch] = "sha1"
    pipe = make_pipe(db, tmp_path, gh, sb, tg)
    pipe.settings.poll_interval_seconds = 600
    await pipe.process(run)
    assert run.state == FAILED
    assert "timed out" in (run.error or "")
    assert sb.cancelled == ["task-1"]
    assert gh.ff_calls == [("feat/x", "sha1")]  # partial progress published


async def test_process_unexpected_exception_still_fails_run(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)

    async def broken_create_app(*a, **kw):
        raise RuntimeError("network down")

    sb.create_app = broken_create_app
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    await make_pipe(db, tmp_path, gh, sb, tg).process(run)
    assert run.state == FAILED
    assert "network down" in (run.error or "")
    assert f"failed:{run.id}" in tg.sent


def _zip_with(path: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(path, data)
    return buf.getvalue()


async def test_report_sends_main_video_small(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, REPORTING)
    run.e2e_status = "passed"
    run.e2e_iteration = 0
    run.e2e_json = json.dumps({"summary": "works", "main_video": ".loop/e2e/main.mp4",
                               "tests": [{"title": "main", "status": "passed",
                                          "video": ".loop/e2e/main.mp4"}]})
    sb.files = [{"path": ".loop/e2e/main.mp4", "type": "file", "size": 100}]
    sb.file_contents[".loop/e2e/main.mp4"] = b"vid"
    await p._report_success(run)
    assert tg.videos == [("main.mp4", f"🎬 Run #{run.id} — main.mp4")]
    assert any("e2e" in c.lower() for c in gh.comments)


async def test_report_large_video_via_export(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, REPORTING)
    run.e2e_status = "passed"
    run.e2e_json = json.dumps({"summary": "works", "main_video": ".loop/e2e/main.mp4",
                               "tests": []})
    sb.files = [{"path": ".loop/e2e/main.mp4", "type": "file", "size": 5 * 1024 * 1024}]
    sb.export_bytes = _zip_with(".loop/e2e/main.mp4", b"bigvid")
    await p._report_success(run)
    assert tg.videos and tg.videos[0][0] == "main.mp4"


async def test_report_oversized_video_skipped(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, REPORTING)
    run.e2e_status = "passed"
    run.e2e_json = json.dumps({"summary": "works", "main_video": ".loop/e2e/main.mp4",
                               "tests": []})
    sb.files = [{"path": ".loop/e2e/main.mp4", "type": "file", "size": 100 * 1024 * 1024}]
    await p._report_success(run)
    assert tg.videos == []
    assert any("skipped" in m for m in tg.sent)


async def test_report_e2e_escalation_labels_and_notifies(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, REPORTING)
    run.e2e_status = "escalated"
    run.e2e_iteration = 2
    run.e2e_json = json.dumps({"summary": "broken", "main_video": None,
                               "tests": [{"title": "main", "status": "failed",
                                          "video": ".loop/e2e/fail-1.mp4"}]})
    sb.files = [{"path": ".loop/e2e/fail-1.mp4", "type": "file", "size": 10}]
    sb.file_contents[".loop/e2e/fail-1.mp4"] = b"failvid"
    await p._report_success(run)
    assert ["loop:needs-review"] in gh.labels_added
    assert f"e2e-escalation:{run.id}:1" in tg.sent
    assert tg.videos and tg.videos[0][0] == "fail-1.mp4"


async def test_report_video_failure_degrades_to_text(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, REPORTING)
    run.e2e_status = "passed"
    run.e2e_json = json.dumps({"summary": "works", "main_video": ".loop/e2e/main.mp4",
                               "tests": []})
    sb.files = [{"path": ".loop/e2e/main.mp4", "type": "file", "size": 100}]
    sb.file_contents[".loop/e2e/main.mp4"] = b"vid"
    tg.video_error = RuntimeError("tg down")
    await p._report_success(run)  # must not raise
    assert any("video" in m for m in tg.sent)


async def seed_full_success(db, tmp_path):
    """Pipeline + fakes + run seeded for a full queued -> done pass."""
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    branch = f"loop/run-{run.id}"
    sb.task_results = [
        {"status": "succeeded", "agent_message": "did the work"},
        {"status": "succeeded",
         "agent_message_final": '{"verdict": "clean", "summary": "ok", "findings": []}'},
    ]
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 2}
    gh.branch_shas[branch] = "sha1"
    return make_pipe(db, tmp_path, gh, sb, tg), gh, sb, tg, run


async def test_process_creates_thread_and_card(db, tmp_path):
    p, gh, sb, tg, run = await seed_full_success(db, tmp_path)
    assert run.state == QUEUED
    await p.process(run)
    saved = await dbmod.get_run(db, run.id)
    assert saved.tg_thread_id == 777
    assert saved.tg_card_message_id == 555
    assert tg.card_states[0] == QUEUED           # initial card
    assert tg.card_states[-1] == DONE            # refreshed at the end
    assert tg.thread_finished


async def test_process_updates_card_on_each_transition(db, tmp_path):
    p, gh, sb, tg, run = await seed_full_success(db, tmp_path)
    await p.process(run)
    # one snapshot per transition target at minimum
    for state in (PREPARING, EXECUTING, PUBLISHING, REPORTING, DONE):
        assert state in tg.card_states


async def test_process_recovered_queued_run_keeps_topic_and_card(db, tmp_path):
    # A restart while still QUEUED must not mint a duplicate topic/card.
    p, gh, sb, tg, run = await seed_full_success(db, tmp_path)
    run.tg_thread_id = 111
    run.tg_card_message_id = 222
    await dbmod.save_run(db, run)
    await p.process(run)
    saved = await dbmod.get_run(db, run.id)
    assert saved.tg_thread_id == 111
    assert saved.tg_card_message_id == 222


async def test_fail_updates_card_and_finishes_thread(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, QUEUED)
    await p.fail(run, "executing", "boom")
    assert FAILED in tg.card_states
    assert tg.thread_finished
    assert any(m.startswith("failed:") or "failed" in m for m in tg.sent)


def seed_approval(gh, tmp_path):
    """seed_ok, but with the pause enabled and a run command for preview."""
    seed_ok(gh, tmp_path)
    gh.files[".loop.yml"] = (gh.files[".loop.yml"]
                             .replace("approval: never", "approval: always")
                             + "run: npm run dev -- --port 3000\n")


async def test_process_pauses_for_approval(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_approval(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    branch = f"loop/run-{run.id}"
    sb.task_results = [
        {"status": "succeeded", "agent_message": "did the work"},   # execute
        {"status": "succeeded",
         "agent_message_final": '{"verdict": "clean", "summary": "ok", "findings": []}'},
    ]
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 2}
    await make_pipe(db, tmp_path, gh, sb, tg).process(run)
    assert run.state == AWAITING_APPROVAL
    assert run.staging_branch == branch
    assert run.preview_url == "https://s-x-3000.preview.test"
    assert run.sandbox_expires_at is not None
    assert run.tg_approval_message_id == 900
    assert f"awaiting:{run.id}" in tg.sent
    assert STAGING in tg.card_states
    assert gh.ff_calls == []                  # PR branch untouched before approve
    assert sb.apps_deleted == []              # the app survives; only the container sleeps
    # The pause sleeps instead of being held awake: the manifest lets the wake
    # path bring the same server back, so 3.5 GB is not parked for two hours.
    assert sb.stopped == [run.sandbox_id]
    assert sb.keepalives == [] or (run.sandbox_id, 120) not in sb.keepalives
    assert any(path == "sandbox.yaml" for _, path, _ in sb.files_written)
    # The preview costs no model call: the server is started through exec, and
    # the URL is only recorded once the port answers.
    assert not any("npm run dev" in t["prompt"] for t in sb.tasks_submitted)
    script = sb.execs[0]["cmd"]
    assert script[:2] == ["sh", "-c"]
    assert "nohup npm run dev -- --port 3000 > .loop/preview.log" in script[2]
    assert any(e["cmd"][0] == "python3" for e in sb.execs[1:])   # port probe


async def test_pause_stays_awake_when_the_preview_cannot_survive_a_sleep(db, tmp_path):
    """No manifest, no sleep. If the platform cannot bring the preview server
    back on its own, stopping the sandbox would turn the link into a permanent
    502 — so the old behaviour (held awake for the whole TTL) has to remain."""
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_approval(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    branch = f"loop/run-{run.id}"
    sb.task_results = [
        {"status": "succeeded", "agent_message": "did the work"},
        {"status": "succeeded",
         "agent_message_final": '{"verdict": "clean", "summary": "ok", "findings": []}'},
    ]
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 2}
    sb.manifest_errors = ["unknown top-level key \"web_process\""]
    await make_pipe(db, tmp_path, gh, sb, tg).process(run)
    assert run.state == AWAITING_APPROVAL
    assert run.preview_url == "https://s-x-3000.preview.test"   # link still published
    assert sb.stopped == []                                     # but never slept
    assert (run.sandbox_id, 120) in sb.keepalives


async def test_process_resumes_from_publishing_after_approve(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    run.state = "publishing"
    run.staging_branch = f"loop/run-{run.id}"
    run.approval_mode = "always"
    run.summary = "did the work"
    await dbmod.save_run(db, run)
    gh.branch_shas[run.staging_branch] = "sha1"
    await make_pipe(db, tmp_path, gh, sb, tg).process(run)
    assert run.state == DONE
    assert gh.ff_calls == [("feat/x", "sha1")]
    assert f"loop/run-{run.id}" in gh.deleted_branches


async def test_process_no_commits_skips_pause(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_approval(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    sb.task_results = [
        {"status": "succeeded", "agent_message": "nothing to do"},
        {"status": "succeeded",
         "agent_message_final": '{"verdict": "clean", "summary": "ok", "findings": []}'},
    ]
    sb.push_resp = {"pushed": False, "reason": "no_local_commits"}
    await make_pipe(db, tmp_path, gh, sb, tg).process(run)
    assert run.state == DONE
    assert "nothing to publish" in (run.summary or "")
    assert f"awaiting:{run.id}" not in tg.sent


async def test_fail_is_noop_when_run_already_cancelled(db, tmp_path):
    pipe, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, EXECUTING)
    fresh = await dbmod.get_run(db, run.id)
    fresh.state = CANCELLED
    await dbmod.save_run(db, fresh)
    await pipe.fail(run, EXECUTING, "task died after cancel")
    assert (await dbmod.get_run(db, run.id)).state == CANCELLED
    assert tg.sent == []  # no failure notification fired


async def test_video_caption_uses_feature_title(db):
    p, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, REPORTING)
    run.pr_title = "feat: web playground"
    run.e2e_status = "passed"
    run.e2e_json = json.dumps({"summary": "works", "main_video": ".loop/e2e/main.mp4",
                               "tests": []})
    sb.files = [{"path": ".loop/e2e/main.mp4", "type": "file", "size": 100}]
    sb.file_contents[".loop/e2e/main.mp4"] = b"vid"
    await p._report_success(run)
    assert tg.videos == [("main.mp4", "🎬 feat: web playground — main.mp4")]


async def test_process_survives_stale_task_conflict_after_restart(db, tmp_path):
    """Recovery mid-stage: sandboxd still runs the pre-restart task, the first
    submit gets 409 — the pipeline waits the stale task out and resubmits."""
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    branch = f"loop/run-{run.id}"
    sb.submit_conflicts = 1
    sb.tasks_listed = [{"id": "stale-1", "status": "running"}]
    sb.task_results = [
        {"status": "succeeded"},                                   # stale task drained
        {"status": "succeeded", "agent_message": "did the work"},  # fresh execute
        {"status": "succeeded",
         "agent_message_final": '{"verdict": "clean", "summary": "ok", "findings": []}'},
    ]
    sb.push_resp = {"pushed": True, "branch": branch, "commits": 2}
    gh.branch_shas[branch] = "sha1"
    await make_pipe(db, tmp_path, gh, sb, tg).process(run)
    assert run.state == DONE
    assert len(sb.tasks_submitted) == 2        # execute (after drain) + review


async def test_run_sandbox_task_drains_stale_task_on_conflict(db):
    from time import monotonic
    pipe, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, EXECUTING)
    sb.submit_conflicts = 1
    sb.tasks_listed = [{"id": "stale-1", "status": "running"}]
    sb.task_results = [
        {"status": "running"}, {"status": "succeeded"},            # stale task poll
        {"status": "succeeded", "agent_message_final": "verdict"}, # our task
    ]
    task, _ = await pipe._run_sandbox_task(run, "review it", 60, monotonic() + 60)
    assert task["agent_message_final"] == "verdict"


async def test_run_sandbox_task_waits_out_not_ready_sandbox(db):
    """Live repro (run #24): a fresh sandbox still seeding answers 409 with
    no in-flight task — the pipeline must keep retrying, not fail the run."""
    from time import monotonic
    pipe, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, EXECUTING)
    sb.submit_conflicts = 2                    # two not-ready rounds
    sb.tasks_listed = []                       # nothing to drain
    sb.task_results = [
        {"status": "succeeded", "agent_message_final": "verdict"}]
    task, _ = await pipe._run_sandbox_task(run, "review it", 60, monotonic() + 60)
    assert task["agent_message_final"] == "verdict"
    assert len(sb.tasks_submitted) == 1


async def test_run_sandbox_task_fails_fast_on_a_sandbox_in_error(db):
    """Run #57: the workspace seed failed at creation (the sandbox image had
    been pruned off the host overnight), so the sandbox answered 409 to every
    submit and never would answer anything else. Waiting it out spent three
    silent hours of the run's budget — a sandbox sandboxd reports as `error`
    must fail the stage immediately instead."""
    import pytest
    from time import monotonic

    from loop_orchestrator.pipeline import RunFailure
    pipe, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, EXECUTING)
    sb.submit_conflicts = 99                   # never becomes ready
    sb.tasks_listed = []
    sb.sandbox_info = {"status": "error"}
    with pytest.raises(RunFailure, match="'error'"):
        await pipe._run_sandbox_task(run, "review it", 600, monotonic() + 600)
    assert sb.tasks_submitted == []            # nothing ever ran


async def test_run_sandbox_task_conflict_past_deadline_reraises(db):
    import pytest
    import httpx
    from time import monotonic
    pipe, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, EXECUTING)
    sb.submit_conflicts = 99                   # never becomes ready
    sb.tasks_listed = []
    with pytest.raises(httpx.HTTPStatusError):
        await pipe._run_sandbox_task(run, "review it", 0, monotonic() + 60)


async def test_notify_awaiting_sends_videos_only_with_message_id(db):
    """Advisor minor #2: when the approval message fails, videos must wait for
    _report_success (its guard fires on tg_approval_message_id is None) —
    otherwise the degraded path would deliver them twice."""
    pipe, gh, sb, tg = make_pipeline(db)
    run = await make_run_in(db, AWAITING_APPROVAL)
    video_calls = []

    async def record_videos(r):
        video_calls.append(r.id)

    pipe._send_e2e_videos = record_videos
    await pipe._notify_awaiting(run)              # FakeTG returns msg id 900
    assert run.tg_approval_message_id == 900
    assert video_calls == [run.id]

    run2 = await make_run_in(db, AWAITING_APPROVAL)

    async def no_message(r):
        return None

    tg.notify_awaiting_approval = no_message
    video_calls.clear()
    await pipe._notify_awaiting(run2)
    assert run2.tg_approval_message_id is None
    assert video_calls == []
