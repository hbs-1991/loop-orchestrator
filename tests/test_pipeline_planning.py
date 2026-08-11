import json

from tests.conftest import FakeGitHub, FakeSandboxd, FakeSettings, FakeTG

from loop_orchestrator import db as dbmod
from loop_orchestrator import issue_tasks as it
from loop_orchestrator.pipeline import Pipeline

LOOP_YML = "specs_dir: docs/specs\n"

PLAN_JSON = json.dumps({"outcome": "plan", "summary": "Two tasks planned."})
QUESTIONS_JSON = json.dumps({"outcome": "questions",
                             "questions": ["Which database?"]})
APPROVED_JSON = json.dumps({"verdict": "approved", "summary": "Solid."})
REVISE_JSON = json.dumps({"verdict": "revise", "summary": "Gaps.",
                          "issues": ["Add a rollback step"]})


def _ok(msg):
    return {"status": "succeeded", "agent_message_final": msg}


async def _make(db, tmp_path, task_results, loop_yml=None):
    gh = FakeGitHub()
    gh.files[".loop.yml"] = loop_yml or LOOP_YML
    sb = FakeSandboxd()
    sb.task_results = list(task_results)
    tg = FakeTG()
    settings = FakeSettings()
    settings.secrets_dir = str(tmp_path)
    pipeline = Pipeline(db=db, settings=settings, gh=gh, sb=sb, tg=tg)
    run = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", "auth")
    await it.upsert_task(db, "o/r", 7, "T", "auth")
    await it.set_run(db, "o/r", 7, run.id)
    await it.set_state(db, "o/r", 7, it.RUNNING)
    return pipeline, run, gh, sb, tg


async def test_advisor_never_approves_escalates_without_publish(db, tmp_path):
    settings_results = [_ok(PLAN_JSON)] + [_ok(REVISE_JSON), _ok(PLAN_JSON)] * 4
    pipeline, run, gh, sb, tg = await _make(db, tmp_path, settings_results)
    pipeline.settings.plan_max_iterations = 1
    await pipeline.process(run)
    assert run.state == "failed"
    assert gh.prs_created == [] and gh.ff_calls == []
    assert (await it.get_task(db, "o/r", 7)).state == it.RUNNING  # tick will mark failed


async def test_prepare_planning_builds_prompt_and_app(db, tmp_path):
    pipeline, run, gh, sb, tg = await _make(db, tmp_path, [_ok(PLAN_JSON),
                                                           _ok(APPROVED_JSON)])
    gh.branch_shas[f"loop/run-{run.id}"] = "plansha"
    await pipeline.process(run)
    assert sb.apps_created[0]["branch"] == "loop/issue-7"
    assert run.spec_path == "docs/specs/issue-7-design.md"
    assert run.plan_path == "docs/plans/issue-7.md"
    first_prompt = sb.tasks_submitted[0]["prompt"]
    assert ".loop/task.md" in first_prompt and "docs/specs/issue-7-design.md" in first_prompt


async def test_advisor_model_and_revise_cycle(db, tmp_path):
    pipeline, run, gh, sb, tg = await _make(
        db, tmp_path,
        [_ok(PLAN_JSON), _ok(REVISE_JSON), _ok(PLAN_JSON), _ok(APPROVED_JSON)])
    gh.branch_shas[f"loop/run-{run.id}"] = "plansha"
    await pipeline.process(run)
    assert run.state == "done"
    prompts = [t["prompt"] for t in sb.tasks_submitted]
    assert "Add a rollback step" in prompts[2]          # revise prompt to planner
    assert sb.tasks_submitted[1]["model"] == "claude-fable-5"  # advisor model
    assert sb.tasks_submitted[1]["continue"] is False   # advisor judges the documents
    # Every round is fresh: `continue` would resume the most recent session,
    # which after the advisor round is the advisor's, not the planner's.
    assert [t["continue"] for t in sb.tasks_submitted] == [False] * 4
    # So the revise prompt has to carry what the session used to.
    assert "docs/specs/issue-7-design.md" in prompts[2]
    assert ".loop/task.md" in prompts[2]


PLANNING_YML = """specs_dir: docs/specs
planning:
  model: claude-opus-5
  advisor:
    model: claude-sonnet-5
    max_iterations: 0
"""


async def test_repo_config_picks_the_planner_and_advisor_models(db, tmp_path):
    pipeline, run, gh, sb, tg = await _make(
        db, tmp_path, [_ok(PLAN_JSON), _ok(APPROVED_JSON)], loop_yml=PLANNING_YML)
    gh.branch_shas[f"loop/run-{run.id}"] = "plansha"
    await pipeline.process(run)
    assert run.state == "done"
    assert sb.tasks_submitted[0]["model"] == "claude-opus-5"    # planner
    assert sb.tasks_submitted[1]["model"] == "claude-sonnet-5"  # advisor
    # Snapshotted onto the Run, so a .loop.yml edited mid-Run cannot change the
    # rules this Run started under.
    fresh = await dbmod.get_run(db, run.id)
    assert (fresh.planner_model, fresh.advisor_model) == ("claude-opus-5",
                                                          "claude-sonnet-5")
    assert fresh.plan_max_iterations == 0


async def test_repo_config_caps_the_revise_rounds(db, tmp_path):
    # max_iterations: 0 — one advisor round, no rewrite, then escalation.
    pipeline, run, gh, sb, tg = await _make(
        db, tmp_path, [_ok(PLAN_JSON), _ok(REVISE_JSON), _ok(PLAN_JSON)],
        loop_yml=PLANNING_YML)
    await pipeline.process(run)
    assert run.state == "failed"
    assert len(sb.tasks_submitted) == 2          # planner + advisor, no revise
    assert "did not approve the plan after 1 iteration" in (run.error or "")


ADVISOR_OFF_YML = "specs_dir: docs/specs\nplanning:\n  advisor:\n    enabled: false\n"


async def test_advisor_can_be_switched_off_per_repo(db, tmp_path):
    pipeline, run, gh, sb, tg = await _make(db, tmp_path, [_ok(PLAN_JSON)],
                                            loop_yml=ADVISOR_OFF_YML)
    gh.branch_shas[f"loop/run-{run.id}"] = "plansha"
    await pipeline.process(run)
    assert run.state == "done"
    assert len(sb.tasks_submitted) == 1          # the planner, and nobody else
    assert gh.prs_created, "the plan must still reach a PR"
    details = [row[0] for row in await db.execute_fetchall(
        "SELECT detail FROM run_events WHERE run_id = ?", (run.id,))]
    assert any("advisor disabled" in d for d in details)


async def test_full_planning_run_publishes_pr_with_loop_run_label(db, tmp_path):
    pipeline, run, gh, sb, tg = await _make(db, tmp_path,
                                            [_ok(PLAN_JSON), _ok(APPROVED_JSON)])
    gh.branch_shas[f"loop/run-{run.id}"] = "plansha"
    await pipeline.process(run)
    assert run.state == "done"
    assert gh.ff_calls == [("loop/issue-7", "plansha")]
    pr = gh.prs_created[0]
    assert pr["head"] == "loop/issue-7" and pr["base"] == "main"
    assert pr["body"].startswith("Closes #7.")
    assert run.pr_number == 501
    assert ["loop:run"] in gh.labels_added
    assert any("#7" in c for c in gh.comments)      # issue comment with PR link
    assert sb.apps_deleted == [run.app_id] or run.app_id in sb.apps_deleted


async def test_questions_outcome_parks_task_as_needs_info(db, tmp_path):
    pipeline, run, gh, sb, tg = await _make(db, tmp_path, [_ok(QUESTIONS_JSON)])
    await pipeline.process(run)
    assert run.state == "done"
    assert run.pr_number == 0 and gh.prs_created == []
    assert (await it.get_task(db, "o/r", 7)).state == it.NEEDS_INFO
    assert any("Which database?" in c for c in gh.comments)
    assert any("more information" in s for s in tg.sent)


async def test_planning_failure_comments_issue_not_pr(db, tmp_path):
    pipeline, run, gh, sb, tg = await _make(
        db, tmp_path, [{"status": "failed", "error_message": "boom"}])
    await pipeline.process(run)
    assert run.state == "failed"
    assert any("Loop run" in c and "failed" in c for c in gh.comments)
    assert "loop:running" not in [l for ls in gh.labels_added for l in ls]


async def test_plan_pr_targets_the_configured_base_branch(db, tmp_path):
    # scheduler.bootstrap forked the issue branch from `staging`; the PR must
    # land there too, or its diff would carry everything staging lacks.
    pipeline, run, gh, sb, tg = await _make(db, tmp_path,
                                            [_ok(PLAN_JSON), _ok(APPROVED_JSON)])
    gh.files[".loop.yml"] = LOOP_YML + "base_branch: staging\n"
    gh.branch_shas[f"loop/run-{run.id}"] = "plansha"
    await pipeline.process(run)
    assert gh.prs_created[0]["base"] == "staging"
