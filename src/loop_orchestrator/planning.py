"""Planning protocol: planner/advisor prompts, JSON parsing, task snapshot."""
from dataclasses import dataclass, field

from .contracts import CONTEXT_DIR, Upstream, render_upstream_section
from .jsonextract import find_json_object
from .loopconfig import plans_dir
from .secrets import source_hint

TASK_FILE = ".loop/task.md"


class PlanningError(Exception):
    pass


@dataclass
class PlannerResult:
    outcome: str  # "plan" | "questions"
    summary: str = ""
    questions: list[str] = field(default_factory=list)


@dataclass
class AdvisorVerdict:
    verdict: str  # "approved" | "revise"
    summary: str = ""
    issues: list[str] = field(default_factory=list)


def plan_paths(specs_dir: str, issue_number: int) -> tuple[str, str]:
    """Spec/plan locations the PR-mode pipeline will find via find_spec_plan_pair."""
    return (f"{specs_dir}/issue-{issue_number}-design.md",
            f"{plans_dir(specs_dir)}/issue-{issue_number}.md")


def _extract_json(text: str, prefer_key: str) -> dict:
    data = find_json_object(text, prefer_key)
    if data is None:
        raise PlanningError("no JSON object in the agent message")
    return data


def parse_planner_output(text: str) -> PlannerResult:
    data = _extract_json(text, "outcome")
    outcome = data.get("outcome")
    if outcome == "plan":
        return PlannerResult(outcome="plan", summary=str(data.get("summary") or ""))
    if outcome == "questions":
        questions = [str(q) for q in (data.get("questions") or []) if str(q).strip()]
        if not questions:
            raise PlanningError("outcome=questions but the questions list is empty")
        return PlannerResult(outcome="questions", questions=questions)
    raise PlanningError(f"unknown planner outcome: {outcome!r}")


def parse_advisor_verdict(text: str) -> AdvisorVerdict:
    data = _extract_json(text, "verdict")
    verdict = data.get("verdict")
    if verdict not in ("approved", "revise"):
        raise PlanningError(f"unknown advisor verdict: {verdict!r}")
    issues = [str(i) for i in (data.get("issues") or []) if str(i).strip()]
    if verdict == "revise" and not issues:
        raise PlanningError("verdict=revise but the issues list is empty")
    return AdvisorVerdict(verdict=verdict, summary=str(data.get("summary") or ""),
                          issues=issues)


PLANNER_OUTPUT_SCHEMA = """{
  "outcome": "plan | questions",
  "summary": "for outcome=plan: 2-4 sentence overview of the planned work",
  "questions": ["for outcome=questions: concrete questions for the issue author"]
}"""

ADVISOR_VERDICT_SCHEMA = """{
  "verdict": "approved | revise",
  "summary": "1-2 sentence overall assessment",
  "issues": ["concrete problems the planner must fix (empty when approved)"]
}"""


def build_planner_prompt(issue_number: int, spec_path: str, plan_path: str,
                         setup_cmd: str | None = None,
                         secrets: dict[str, str] | None = None) -> str:
    setup_line = (f"First install the project dependencies with `{setup_cmd}`.\n"
                  if setup_cmd else "")
    return (
        "You are a planning agent for this repository.\n"
        f"The task is described in {TASK_FILE} — a snapshot of GitHub issue "
        f"#{issue_number} including its discussion thread.\n\n"
        + source_hint(secrets or {}) + setup_line +
        "Study the repository and the task, then produce two documents:\n"
        f"1. Specification (what to build, why, acceptance criteria): {spec_path}\n"
        f"   Use the `writing-specs` skill — it defines the required structure "
        "and the self-review checklist.\n"
        f"2. Implementation plan (ordered tasks, files to touch, test steps): {plan_path}\n"
        "   Use the `writing-plans` skill — bite-sized TDD tasks, exact paths, "
        "real code in every step, no placeholders.\n"
        "Both skills are installed in this sandbox; follow them, and save each "
        "document at exactly the path above rather than the skill's default.\n\n"
        "The plan is executed by an autonomous agent that cannot ask you anything, "
        "so every gap you leave becomes a guess it makes on its own.\n"
        "An interface you do not own — an API of another service or repository — "
        "may be planned against exactly three sources: code in this repository, "
        f"files under `{CONTEXT_DIR}/`, and the `## Upstream dependencies` section "
        f"of {TASK_FILE}. Read them before you write an endpoint, a path, a field "
        "name or a status code.\n"
        "If what the task needs is in none of the three, do not invent a "
        "plausible one: a guessed interface passes review and fails only when the "
        "code runs. Ask instead — return the questions outcome naming the "
        "endpoint you could not confirm.\n\n"
        "Write both files and make a single git commit containing ONLY them. "
        "Never commit dependency lockfiles (uv.lock, package-lock.json, "
        "pnpm-lock.yaml, yarn.lock, poetry.lock, Cargo.lock): installing "
        "dependencies may rewrite one, and a lockfile regenerated by a "
        "different tool version has already shipped broken dev pins to the "
        "trunk once — restore any changed lockfile with `git checkout -- "
        "<file>` before committing.\n"
        "Do not git push. Do not switch branches. Do not implement the feature itself.\n"
        "If the issue is critically underspecified and you cannot plan responsibly, "
        "write no files and ask the author instead.\n\n"
        "Your FINAL message must be a single JSON object and nothing else, "
        "matching exactly this schema:\n"
        f"{PLANNER_OUTPUT_SCHEMA}"
    )


def build_planner_revise_prompt(verdict: AdvisorVerdict, issue_number: int,
                                spec_path: str, plan_path: str) -> str:
    """Revise round for the planner — a fresh session, so self-contained.

    It cannot continue the session that wrote the documents: sandboxd resumes
    *the most recent* session and the advisor ran after the planner, so
    `continue` would hand the planner the advisor's context. Nor is resuming
    worth chasing — an advisor round outlives the five-minute prompt cache, so
    the whole inherited context would be re-billed at write price
    ([[decisions/0013-one-session-per-stage]]). Re-reading three files costs
    less, and a planner that does not remember defending the documents edits
    them more honestly.

    No setup command: the dependencies were installed in round 0 and this is
    the same sandbox.
    """
    issues = "\n".join(f"- {i}" for i in verdict.issues)
    return (
        "You are a planning agent for this repository.\n"
        "An earlier round of this run wrote a specification and an "
        "implementation plan for GitHub issue "
        f"#{issue_number} (the task is in {TASK_FILE}). The Implementor "
        "Advisor reviewed them and requires changes before implementation can "
        "start.\n\n"
        f"Specification: {spec_path}\n"
        f"Plan: {plan_path}\n\n"
        f"Advisor summary: {verdict.summary}\n"
        f"Issues to address:\n{issues}\n\n"
        f"Read {TASK_FILE} and both documents first — you are in a new session "
        "and have not seen them yet — then update them so every issue above is "
        "resolved. Keep the structure the `writing-specs` and `writing-plans` "
        "skills define, and keep each document at exactly the path above.\n"
        "The plan is executed by an autonomous agent that cannot ask you "
        "anything, so every gap you leave becomes a guess it makes on its own.\n"
        "Commit only those two files. Never commit dependency lockfiles "
        "(uv.lock, package-lock.json, pnpm-lock.yaml, yarn.lock, poetry.lock, "
        "Cargo.lock): restore any changed lockfile with `git checkout -- "
        "<file>` before committing.\n"
        "Do not git push. Do not switch branches. Do not implement the feature "
        "itself.\n\n"
        "Finish with the same JSON schema as before:\n"
        f"{PLANNER_OUTPUT_SCHEMA}"
    )


def build_advisor_prompt(spec_path: str, plan_path: str) -> str:
    return (
        "You are the Implementor Advisor: a senior engineer who decides whether "
        "a prepared plan is ready to be implemented by an autonomous agent.\n"
        f"Task: {TASK_FILE}\n"
        f"Specification: {spec_path}\n"
        f"Plan: {plan_path}\n\n"
        "Read all three documents and check them against the repository: "
        "feasibility, completeness, hidden risks, missing acceptance criteria, "
        "and whether the plan actually solves the issue.\n\n"
        "Check the specification for: placeholders and unfinished sections; "
        "internal contradictions; requirements ambiguous enough that two "
        "engineers would build different things; acceptance criteria that "
        "cannot be observed or tested; unrequested scope; and claims about the "
        "codebase that the code does not support.\n"
        "Check the plan for: a task covering every requirement of the spec; "
        "bite-sized TDD steps rather than vague instructions; exact file paths; "
        "real code in steps that change code; commands that actually work in "
        "this repository; and consistent names and signatures across tasks.\n"
        "Check every external interface the documents rely on: each endpoint, "
        "field name and status code must be traceable to code in this "
        f"repository, to a file under `{CONTEXT_DIR}/`, or to the "
        f"`## Upstream dependencies` section of {TASK_FILE}. One that is "
        "traceable to none of them is invented, however plausible it reads — "
        "raise it as an issue naming that endpoint.\n"
        "Only raise issues that would produce a wrong or stalled implementation. "
        "Wording preferences and unevenly detailed sections are not issues — "
        "approve when the pair is good enough to implement from.\n"
        "Do NOT modify, commit or push anything — you only advise.\n\n"
        "Your FINAL message must be a single JSON object and nothing else, "
        "matching exactly this schema:\n"
        f"{ADVISOR_VERDICT_SCHEMA}"
    )


def build_task_file(issue: dict, comments: list[dict],
                    upstreams: "list[Upstream] | tuple" = ()) -> str:
    labels = ", ".join(
        (lbl["name"] if isinstance(lbl, dict) else str(lbl))
        for lbl in (issue.get("labels") or []))
    lines = [f"# Issue #{issue['number']}: {issue.get('title') or ''}", ""]
    if labels:
        lines += [f"Labels: {labels}", ""]
    lines += [issue.get("body") or "(no description)", ""]
    if comments:
        lines += ["## Discussion", ""]
        for c in comments:
            author = (c.get("user") or {}).get("login") or "unknown"
            lines += [f"**{author}:**", c.get("body") or "", ""]
    section = render_upstream_section(list(upstreams))
    if section:
        lines += [section]
    return "\n".join(lines)
