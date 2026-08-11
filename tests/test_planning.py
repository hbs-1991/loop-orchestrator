import pytest

from loop_orchestrator.contracts import Upstream
from loop_orchestrator.planning import (
    PlanningError,
    build_advisor_prompt,
    build_planner_prompt,
    build_planner_revise_prompt,
    build_task_file,
    parse_advisor_verdict,
    parse_planner_output,
    plan_paths,
)


def test_plan_paths_follow_loopconfig_convention():
    assert plan_paths("docs/superpowers/specs", 7) == (
        "docs/superpowers/specs/issue-7-design.md",
        "docs/superpowers/plans/issue-7.md")


def test_parse_planner_output_plan():
    out = parse_planner_output('done\n{"outcome": "plan", "summary": "Two tasks."}')
    assert (out.outcome, out.summary, out.questions) == ("plan", "Two tasks.", [])


def test_parse_planner_output_questions():
    out = parse_planner_output('{"outcome": "questions", "questions": ["Which DB?"]}')
    assert out.outcome == "questions"
    assert out.questions == ["Which DB?"]


def test_parse_planner_output_rejects_garbage():
    with pytest.raises(PlanningError):
        parse_planner_output("no json here")
    with pytest.raises(PlanningError):
        parse_planner_output('{"outcome": "maybe"}')
    with pytest.raises(PlanningError):
        parse_planner_output('{"outcome": "questions", "questions": []}')


def test_parse_advisor_verdict():
    v = parse_advisor_verdict('{"verdict": "revise", "summary": "Gaps.", '
                              '"issues": ["No rollback step"]}')
    assert (v.verdict, v.issues) == ("revise", ["No rollback step"])
    with pytest.raises(PlanningError):
        parse_advisor_verdict('{"verdict": "revise", "issues": []}')


def test_parse_survives_prose_braces_around_verdict():
    # Live repro (run #20): analytic prose with inline `{op: ...}` braces
    # before the fenced JSON broke the old greedy-regex extraction.
    advisor = ("The e2e tests post `{op: ...}` directly, so the alias is safe.\n"
               "```json\n"
               '{"verdict": "approved", "summary": "Solid.", "issues": []}\n'
               "```")
    assert parse_advisor_verdict(advisor).verdict == "approved"
    planner = ('I resolved `{"fn": ...}` handling as documented.\n'
               '{"outcome": "plan", "summary": "Done."}')
    assert parse_planner_output(planner).outcome == "plan"


def test_prompts_mention_paths_and_schema():
    p = build_planner_prompt(7, "s/issue-7-design.md", "p/issue-7.md", "make setup")
    assert ".loop/task.md" in p and "s/issue-7-design.md" in p and "make setup" in p
    a = build_advisor_prompt("s/issue-7-design.md", "p/issue-7.md")
    assert "approved | revise" in a
    r = build_planner_revise_prompt(
        parse_advisor_verdict(
            '{"verdict": "revise", "summary": "s", "issues": ["fix X"]}'),
        7, "s/issue-7-design.md", "p/issue-7.md")
    assert "fix X" in r


def test_revise_prompt_is_self_contained():
    # It runs in a fresh session (sandboxd would otherwise resume the advisor's,
    # not the planner's), so everything the session used to carry is in the text.
    r = build_planner_revise_prompt(
        parse_advisor_verdict(
            '{"verdict": "revise", "summary": "thin", "issues": ["fix X"]}'),
        7, "s/issue-7-design.md", "p/issue-7.md")
    assert ".loop/task.md" in r
    assert "s/issue-7-design.md" in r and "p/issue-7.md" in r
    assert "#7" in r
    assert "uv.lock" in r and "git checkout --" in r
    assert "Do not git push" in r
    assert "outcome" in r  # the JSON schema survives the rewrite


def test_planner_prompt_forbids_lockfiles():
    # The planner shipped a regenerated uv.lock in PR #13 (dev group dropped,
    # ruff unpinned) — the commit must stay spec+plan only.
    p = build_planner_prompt(7, "specs/issue-7-design.md", "plans/issue-7.md",
                             "uv sync --frozen")
    assert "uv.lock" in p and "lockfile" in p.lower()
    assert "git checkout --" in p


def test_planner_prompt_directs_to_the_baked_skills():
    # The skills are installed in the sandbox image (deploy/sandbox-image/skills);
    # the prompt is the only thing that makes the planner reach for them, and it
    # must override their default save locations with the orchestrator's paths.
    p = build_planner_prompt(7, "specs/issue-7-design.md", "plans/issue-7.md")
    assert "`writing-specs`" in p and "`writing-plans`" in p
    assert "rather than the skill's default" in p
    # The hard constraints must survive the added guidance.
    assert "Do not git push" in p and "Do not switch branches" in p
    assert p.rstrip().endswith("}")


def test_advisor_prompt_checks_spec_and_plan_quality():
    a = build_advisor_prompt("specs/issue-7-design.md", "plans/issue-7.md")
    assert "placeholders" in a and "acceptance criteria" in a
    assert "exact file paths" in a
    # Calibration: the Advisor must not bounce a usable plan over style.
    assert "Wording preferences" in a
    assert "Do NOT modify, commit or push anything" in a


def test_planner_prompt_forbids_inventing_an_interface():
    p = build_planner_prompt(7, "s.md", "p.md")
    assert ".loop/context/" in p
    assert "Upstream dependencies" in p
    assert "do not invent" in p.lower()
    assert '"questions"' in p


def test_advisor_prompt_demands_traceable_endpoints():
    a = build_advisor_prompt("s.md", "p.md")
    assert ".loop/context/" in a
    assert "traceable" in a.lower()


def test_task_file_carries_the_upstream_section():
    text = build_task_file(
        {"number": 13, "title": "F", "body": "B", "labels": []}, [],
        [Upstream(repo="o/backend", number=12, contract_md="### POST /v1/x")])
    assert "# Issue #13" in text
    assert "## Upstream dependencies" in text
    assert "### POST /v1/x" in text


def test_task_file_without_upstreams_is_unchanged():
    text = build_task_file({"number": 13, "title": "F", "body": "B", "labels": []}, [])
    assert "Upstream dependencies" not in text


def test_build_task_file_snapshot():
    text = build_task_file(
        {"number": 7, "title": "Fix login", "body": "Steps...",
         "labels": [{"name": "loop:ready"}, {"name": "loop:lane:auth"}]},
        [{"user": {"login": "alice"}, "body": "Also check SSO."}])
    assert "# Issue #7: Fix login" in text
    assert "loop:lane:auth" in text
    assert "alice" in text and "Also check SSO." in text
