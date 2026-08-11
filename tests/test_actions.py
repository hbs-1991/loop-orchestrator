import pytest

from loop_orchestrator import db as dbmod
from loop_orchestrator import issue_tasks as it
from loop_orchestrator.actions import ActionError, Actions
from loop_orchestrator.models import (
    AWAITING_APPROVAL,
    CANCELLED,
    DONE,
    EXECUTING,
    FAILED,
    PLANNING,
    PUBLISHING,
)
from loop_orchestrator.pipeline import Pipeline

from tests.conftest import FakeGitHub, FakeSandboxd, FakeSettings, FakeTG
from tests.test_webhook import FakeWorker


def make_actions(db):
    gh, sb, tg, worker = FakeGitHub(), FakeSandboxd(), FakeTG(), FakeWorker()
    pipeline = Pipeline(db=db, settings=FakeSettings(), gh=gh, sb=sb, tg=tg)
    return Actions(db=db, settings=FakeSettings(), gh=gh, sb=sb, tg=tg,
                   worker=worker, pipeline=pipeline), gh, sb, tg, worker


async def make_run_in(db, state, **kw):
    run = await dbmod.create_run(db, "o/r", 5, "feat/x", pr_title="feat: x")
    run.state = state
    run.app_id = "app-1"
    run.sandbox_id = "sb-app-1"
    for k, v in kw.items():
        setattr(run, k, v)
    await dbmod.save_run(db, run)
    return run


async def test_approve_moves_to_publishing_and_enqueues(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, AWAITING_APPROVAL, staging_branch="loop/run-1")
    result = await actions.approve(run.id, actor=1)
    assert "approved" in result
    fresh = await dbmod.get_run(db, run.id)
    assert fresh.state == PUBLISHING
    assert worker.enqueued == [run.id]
    events = await dbmod.events_for_run(db, run.id)
    assert events[-1][0] == PUBLISHING


async def test_approve_rejects_wrong_state(db):
    actions, *_ = make_actions(db)
    run = await make_run_in(db, EXECUTING)
    with pytest.raises(ActionError, match="already executing"):
        await actions.approve(run.id, actor=1)


async def test_discard_cancels_and_keeps_branch(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, AWAITING_APPROVAL, staging_branch="loop/run-1")
    await actions.discard(run.id, actor=1)
    fresh = await dbmod.get_run(db, run.id)
    assert fresh.state == CANCELLED
    assert fresh.app_id is None
    assert sb.apps_deleted == ["app-1"]
    assert f"cancelled:{run.id}" in tg.sent
    assert "loop/run-1" not in gh.deleted_branches  # staged work preserved


async def test_revise_resubmits_and_resets_cycles(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, AWAITING_APPROVAL, staging_branch="loop/run-1",
                            review_status="clean", review_iteration=1,
                            e2e_status="passed", e2e_iteration=2,
                            spec_path="docs/spec.md", plan_path="docs/plan.md",
                            test_cmd="pytest -q",
                            sandbox_expires_at="2026-08-03 10:00:00")
    await actions.revise(run.id, actor=1, feedback="make the button blue")
    fresh = await dbmod.get_run(db, run.id)
    assert fresh.state == EXECUTING
    assert fresh.staging_branch is None
    assert "loop/run-1" in gh.deleted_branches       # temp branch freed for re-push
    assert fresh.review_status is None and fresh.review_iteration == 0
    assert fresh.e2e_status is None and fresh.e2e_iteration == 0
    assert fresh.sandbox_expires_at is None
    task = sb.tasks_submitted[-1]
    assert "make the button blue" in task["prompt"]
    # review ran, so its session is the most recent one: continuing would land
    # in the reviewer's, not the executor's. Fresh, with the context restated.
    assert task["continue"] is False
    assert "origin/feat/x..HEAD" in task["prompt"]
    assert "docs/spec.md" in task["prompt"] and "pytest -q" in task["prompt"]
    assert fresh.task_id is not None
    assert worker.enqueued == [run.id]


async def test_revise_resumes_the_executor_session_when_it_is_the_last_one(db):
    """Nothing runs after the executor when both verification stages are off,
    so `continue` reaches its session and the prompt can stay short."""
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, AWAITING_APPROVAL, staging_branch="loop/run-1",
                            review_enabled=False, e2e_enabled=False,
                            test_cmd="pytest -q")
    await actions.revise(run.id, actor=1, feedback="make the button blue")
    task = sb.tasks_submitted[-1]
    assert task["continue"] is True
    assert "make the button blue" in task["prompt"] and "pytest -q" in task["prompt"]
    assert "fresh session" not in task["prompt"]


async def test_revise_fails_after_preview_expiry(db):
    actions, *_ = make_actions(db)
    run = await make_run_in(db, AWAITING_APPROVAL, app_id=None, sandbox_id=None)
    with pytest.raises(ActionError, match="expired"):
        await actions.revise(run.id, actor=1, feedback="x")


async def test_cancel_rescues_work_to_staging(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, EXECUTING, task_id="task-1")
    sb.push_resp = {"pushed": True, "branch": f"loop/run-{run.id}", "commits": 1}
    result = await actions.cancel(run.id, actor=1)
    assert "cancelled" in result
    fresh = await dbmod.get_run(db, run.id)
    assert fresh.state == CANCELLED
    assert fresh.staging_branch == f"loop/run-{run.id}"
    assert sb.cancelled == ["task-1"]
    assert gh.ff_calls == []  # the PR branch is never touched by cancel
    assert sb.apps_deleted == ["app-1"]


async def test_cancel_planning_run_comments_issue_without_pr_labels(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", None)
    run.state = PLANNING
    await dbmod.save_run(db, run)
    result = await actions.cancel(run.id, actor=1)
    assert "cancelled" in result
    assert "loop:running" not in gh.labels_removed  # never set on planning runs
    assert any("cancelled" in c for c in gh.comments)  # notice lands on the issue


async def test_restart_creates_fresh_run(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, FAILED)
    result = await actions.restart(run.id, actor=1)
    assert "restarted" in result
    assert len(worker.enqueued) == 1
    new = await dbmod.get_run(db, worker.enqueued[0])
    assert new.id != run.id and new.pr_number == 5 and new.pr_title == "feat: x"


async def test_restart_rejected_while_active_run_exists(db):
    actions, *_ = make_actions(db)
    old = await make_run_in(db, FAILED)
    await make_run_in(db, EXECUTING)  # a second, active run on the same PR
    with pytest.raises(ActionError, match="already active"):
        await actions.restart(old.id, actor=1)


async def test_merge_squashes_and_cleans_branches(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, DONE, staging_branch="loop/run-1")
    result = await actions.merge(run.id, actor=1)
    assert "merged" in result
    assert gh.merges == [(5, "feat: x (#5)")]
    assert "feat/x" in gh.deleted_branches
    assert (await dbmod.get_run(db, run.id)).merged_at is not None
    # the run's topic is finalised again: renamed to the merged marker + closed
    assert f"thread-finished:{run.id}:{DONE}" in tg.sent
    with pytest.raises(ActionError, match="already merged"):
        await actions.merge(run.id, actor=1)


async def test_merge_deploy_labels_before_merging(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, DONE, staging_branch="loop/run-1")
    result = await actions.merge_deploy(run.id, actor=1)
    assert "merged" in result and "promote:staging" in result
    assert gh.labels_added == [["promote:staging"]]
    assert gh.merges == [(5, "feat: x (#5)")]
    assert (await dbmod.get_run(db, run.id)).merged_at is not None


async def test_merge_deploy_aborts_when_labelling_fails(db):
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.label_error = RuntimeError("label API down")
    with pytest.raises(ActionError, match="could not label"):
        await actions.merge_deploy(run.id, actor=1)
    assert gh.merges == []                          # nothing was merged
    assert (await dbmod.get_run(db, run.id)).merged_at is None


async def test_merge_deploy_unlabels_on_merge_failure(db):
    from loop_orchestrator.clients.github import MergeError
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.merge_error = MergeError("Pull Request is not mergeable")
    with pytest.raises(ActionError, match="not mergeable"):
        await actions.merge_deploy(run.id, actor=1)
    # the deploy trigger must not linger on an unmerged PR
    assert "promote:staging" in gh.labels_removed
    assert (await dbmod.get_run(db, run.id)).merged_at is None


async def test_merge_refused_while_ci_is_red(db):
    # PR #13 merged with failing CI and shipped a broken uv.lock to main —
    # the gate names the red checks and leaves the PR untouched.
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": True, "mergeable_state": "unstable",
                  "base": {"ref": "main"}, "head": {"sha": "headsha"}}
    gh.check_runs = [
        {"name": "CI / test", "status": "completed", "conclusion": "failure"},
        {"name": "lint", "status": "completed", "conclusion": "success"},
    ]
    with pytest.raises(ActionError, match="CI is red.*CI / test"):
        await actions.merge(run.id, actor=1)
    assert gh.merges == []
    assert (await dbmod.get_run(db, run.id)).merged_at is None


async def test_merge_deploy_red_ci_adds_no_label(db):
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": True, "mergeable_state": "unstable",
                  "base": {"ref": "main"}, "head": {"sha": "headsha"}}
    gh.check_runs = [{"name": "ci", "status": "completed",
                      "conclusion": "cancelled"}]
    with pytest.raises(ActionError, match="CI is red"):
        await actions.merge_deploy(run.id, actor=1)
    assert gh.labels_added == []


async def test_merge_waits_for_running_checks(db):
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": True, "mergeable_state": "unstable",
                  "base": {"ref": "main"}, "head": {"sha": "headsha"}}
    gh.check_runs = [{"name": "ci", "status": "in_progress",
                      "conclusion": None}]
    with pytest.raises(ActionError, match="still running"):
        await actions.merge(run.id, actor=1)
    assert gh.merges == []


async def test_merge_waits_for_a_required_check_that_has_not_started(db):
    # Run #40's merge landed in the window right after the conflict resolver
    # fast-forwarded the branch: GitHub had not created the new head's check
    # runs yet, the empty list read as "this repo has no CI", and the ruleset
    # refused the merge with "Required status check ci is queued".
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": True, "mergeable_state": "clean",
                  "base": {"ref": "main"}, "head": {"sha": "freshsha"}}
    gh.check_runs = []
    gh.required_check_names = ["ci"]
    with pytest.raises(ActionError, match="still running.*ci"):
        await actions.merge(run.id, actor=1)
    assert gh.merges == []


async def test_merge_passes_green_and_checkless_repos(db):
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": True, "mergeable_state": "clean",
                  "base": {"ref": "main"}, "head": {"sha": "headsha"}}
    gh.check_runs = [
        {"name": "ci", "status": "completed", "conclusion": "success"},
        {"name": "optional", "status": "completed", "conclusion": "skipped"},
    ]
    await actions.merge(run.id, actor=1)
    assert gh.merges == [(5, "feat: x (#5)")]


async def test_merge_behind_updates_branch_instead_of_merging(db):
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": True, "mergeable_state": "behind",
                  "base": {"ref": "main"}}
    result = await actions.merge(run.id, actor=1)
    assert "updated" in result and "press" in result
    assert gh.branch_updates == [5]
    assert gh.merges == []
    assert (await dbmod.get_run(db, run.id)).merged_at is None


async def test_merge_detects_a_stale_branch_a_clean_state_hides(db):
    """The work repos dropped the strict rule, so GitHub stops sending
    `mergeable_state: "behind"` — a branch that is commits behind main arrives
    labelled `clean` with green checks. Those checks measured a tree that will
    not exist after the merge, so the state is computed instead of trusted."""
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": True, "mergeable_state": "clean",
                  "base": {"ref": "main"},
                  "head": {"sha": "headsha", "ref": "feat/x"}}
    gh.check_runs = [{"name": "ci", "status": "completed",
                      "conclusion": "success"}]
    gh.behind = 2

    result = await actions.merge(run.id, actor=1)

    assert gh.compares == [("main", "feat/x")]
    assert "updated" in result and "press" in result
    assert gh.branch_updates == [5]
    assert gh.merges == []


async def test_merge_does_not_sync_a_branch_that_is_already_current(db):
    """behind_by == 0 must merge on the first press: an empty merge commit is
    itself a push, and costs the full CI run this check exists to save."""
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": True, "mergeable_state": "clean",
                  "base": {"ref": "main"},
                  "head": {"sha": "headsha", "ref": "feat/x"}}
    gh.check_runs = [{"name": "ci", "status": "completed",
                      "conclusion": "success"}]
    gh.behind = 0

    await actions.merge(run.id, actor=1)

    assert gh.branch_updates == []
    assert gh.merges == [(5, "feat: x (#5)")]


async def test_gate_counts_progress_against_the_required_checks(db):
    """The button says "2/3" of what actually holds the merge, not of whatever
    happened to start — an unrequired check must not inflate the denominator."""
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": True, "mergeable_state": "clean",
                  "base": {"ref": "main"},
                  "head": {"sha": "headsha", "ref": "feat/x"}}
    gh.required_check_names = ["gates", "tests-selective", "image"]
    gh.check_runs = [
        {"name": "gates", "status": "completed", "conclusion": "success"},
        {"name": "tests-selective", "status": "completed", "conclusion": "success"},
        {"name": "image", "status": "in_progress"},
        {"name": "Claude Code Review", "status": "completed", "conclusion": "success"},
    ]

    g = await actions.gate(run)

    assert (g.state, g.done, g.total) == ("checks_pending", 2, 3)


async def test_update_branch_refuses_once_the_branch_is_current(db):
    """The button promises one thing. If the gate turned clean between the
    repaint and the press, updating would create an empty merge commit — and
    merging instead would be a merge the user never asked for."""
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": True, "mergeable_state": "clean",
                  "base": {"ref": "main"},
                  "head": {"sha": "headsha", "ref": "feat/x"}}
    gh.check_runs = [{"name": "ci", "status": "completed", "conclusion": "success"}]
    gh.behind = 0

    with pytest.raises(ActionError, match="not behind"):
        await actions.update_branch(run.id, actor=1)
    assert gh.branch_updates == [] and gh.merges == []

    gh.behind = 3
    result = await actions.update_branch(run.id, actor=1)
    assert gh.branch_updates == [5] and gh.merges == []
    assert "behind" in result


async def test_merge_conflicts_resolves_then_merges(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": False, "mergeable_state": "dirty",
                  "base": {"ref": "main"}}
    sb.task_results = [{"status": "succeeded", "agent_message_final":
                        '{"resolved": true, "files": ["docs/wiki/log.md"], '
                        '"notes": "kept both entries"}'}]
    sb.push_resp = {"pushed": True, "branch": f"loop/run-{run.id}-sync"}
    gh.branch_shas[f"loop/run-{run.id}-sync"] = "syncsha"

    result = await actions.merge(run.id, actor=1)
    assert "conflict-resolution agent" in result
    assert gh.merges == []                      # nothing merged yet

    sync = actions._syncs[run.id]
    gh.pr_info = {"mergeable": True, "mergeable_state": "clean",
                  "base": {"ref": "main"}}      # resolution made it clean
    await sync

    assert sb.apps_created[-1]["branch"] == "feat/x"       # PR head checkout
    # The token reaches the agent as a file: app config never enters a sandbox,
    # and a *_TOKEN env var is scrubbed from the agent's environment.
    sandbox, path, content = sb.files_written[-1]
    assert path == ".loop/secrets.env" and "GIT_SYNC_TOKEN=gh-tok" in content
    assert ("feat/x", "syncsha") in gh.ff_calls            # PR branch ff'd
    assert f"loop/run-{run.id}-sync" in gh.deleted_branches
    assert "app-1" in sb.apps_deleted
    assert gh.merges == [(5, "feat: x (#5)")]              # merge retried
    assert any("resolved" in s and "docs/wiki/log.md" in s for s in tg.sent)


async def test_merge_conflict_resolution_failure_is_reported(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": False, "mergeable_state": "dirty",
                  "base": {"ref": "main"}}
    sb.task_results = [{"status": "succeeded", "agent_message_final":
                        '{"resolved": false, "files": [], '
                        '"notes": "binary conflict"}'}]
    await actions.merge(run.id, actor=1)
    await actions._syncs[run.id]
    assert gh.merges == []
    assert (await dbmod.get_run(db, run.id)).merged_at is None
    assert "app-1" in sb.apps_deleted                       # no leak
    assert any("resolution failed" in s and "binary conflict" in s
               for s in tg.sent)


async def test_merge_deploy_labels_only_after_resolution(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": False, "mergeable_state": "dirty",
                  "base": {"ref": "main"}}
    sb.task_results = [{"status": "succeeded", "agent_message_final":
                        '{"resolved": true, "files": ["a.py"], "notes": "ok"}'}]
    sb.push_resp = {"pushed": True, "branch": f"loop/run-{run.id}-sync"}
    gh.branch_shas[f"loop/run-{run.id}-sync"] = "syncsha"
    await actions.merge_deploy(run.id, actor=1)
    assert gh.labels_added == []                # not before the real merge
    sync = actions._syncs[run.id]
    gh.pr_info = {"mergeable": True, "mergeable_state": "clean",
                  "base": {"ref": "main"}}
    await sync
    assert gh.labels_added == [["promote:staging"]]
    assert gh.merges == [(5, "feat: x (#5)")]


async def test_merge_conflicts_second_click_rejected_while_resolving(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.pr_info = {"mergeable": False, "mergeable_state": "dirty",
                  "base": {"ref": "main"}}
    sb.task_results = [{"status": "succeeded", "agent_message_final":
                        '{"resolved": false, "files": [], "notes": "x"}'}]
    await actions.merge(run.id, actor=1)
    with pytest.raises(ActionError, match="already running"):
        await actions.merge(run.id, actor=1)
    await actions._syncs[run.id]


async def test_merge_error_reported_not_swallowed(db):
    from loop_orchestrator.clients.github import MergeError
    actions, gh, *_ = make_actions(db)
    run = await make_run_in(db, DONE)
    gh.merge_error = MergeError("Pull Request is not mergeable")
    with pytest.raises(ActionError, match="not mergeable"):
        await actions.merge(run.id, actor=1)
    assert (await dbmod.get_run(db, run.id)).merged_at is None


async def test_approve_repaints_card_immediately(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, AWAITING_APPROVAL, staging_branch="loop/run-1",
                            tg_card_message_id=1)
    await actions.approve(run.id, actor=1)
    assert PUBLISHING in tg.card_states


async def test_revise_starts_a_fresh_card(db):
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, AWAITING_APPROVAL, staging_branch="loop/run-1",
                            tg_card_message_id=1)
    await actions.revise(run.id, actor=1, feedback="x")
    fresh = await dbmod.get_run(db, run.id)
    # the old card is abandoned; a new one is posted for the fix cycle
    assert fresh.tg_card_message_id == 555          # FakeTG.send_card
    assert EXECUTING in tg.card_states


async def test_revise_wakes_the_sleeping_pause_sandbox(db):
    """The pause stops its sandbox, so a revise has to wake it before submitting
    — otherwise the submit lands on a stopped container and the button reports
    'could not reach the sandbox' on a Run that is perfectly alive."""
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, AWAITING_APPROVAL, staging_branch="loop/run-1")
    sb.sandbox_info = {"status": "stopped"}
    await actions.revise(run.id, actor=1, feedback="make the button blue")
    assert sb.started == [run.sandbox_id]
    assert sb.tasks_submitted, "the revise task must reach the woken sandbox"


async def test_revise_submit_failure_keeps_staged_branch(db):
    """Advisor minor #1: the temp branch must survive a failed revise submit,
    so Approve still works after the sandbox dies mid-revise."""
    actions, gh, sb, tg, worker = make_actions(db)
    run = await make_run_in(db, AWAITING_APPROVAL, staging_branch="loop/run-1")
    sb.submit_conflicts = 1                       # submit_task raises
    with pytest.raises(ActionError, match="could not reach"):
        await actions.revise(run.id, actor=1, feedback="x")
    fresh = await dbmod.get_run(db, run.id)
    assert fresh.state == AWAITING_APPROVAL
    assert fresh.staging_branch == "loop/run-1"
    assert "loop/run-1" not in gh.deleted_branches
    assert worker.enqueued == []


class FakeWorkerRecorder:
    def __init__(self):
        self.enqueued: list[int] = []

    def enqueue(self, run_id):
        self.enqueued.append(run_id)


async def test_restart_planning_run_recreates_chain(db):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    worker = FakeWorkerRecorder()
    actions = Actions(db=db, settings=FakeSettings(), gh=gh, sb=sb, tg=tg,
                      worker=worker, pipeline=None)
    old = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", "auth")
    old.state = FAILED
    await dbmod.save_run(db, old)
    await it.upsert_task(db, "o/r", 7, "T", "auth")
    await it.set_run(db, "o/r", 7, old.id)
    await it.set_state(db, "o/r", 7, it.FAILED)

    msg = await actions.restart(old.id, actor=1)

    assert "restarted" in msg
    task = await it.get_task(db, "o/r", 7)
    assert task.state == it.RUNNING and task.run_id != old.id
    new = await dbmod.get_run(db, task.run_id)
    assert (new.kind, new.issue_number, new.lane) == ("planning", 7, "auth")
    assert "loop:failed" in gh.labels_removed
    assert worker.enqueued == [new.id]
