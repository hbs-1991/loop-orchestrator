"""Review verdict protocol: prompts, JSON verdict parsing, PR-comment report."""
import json
from dataclasses import asdict, dataclass, field

from .jsonextract import find_json_object


class VerdictError(Exception):
    pass


@dataclass
class Finding:
    severity: str
    file: str
    title: str
    detail: str = ""
    line: int | None = None


@dataclass
class Verdict:
    verdict: str  # "clean" | "findings"
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)


# Shared verbatim by every stage prompt (executor in pipeline.py, reviewer, e2e).
# The cost constraint is identical everywhere, so the wording is too — a stage
# that phrases it differently is a stage the model treats as a different rule.
# Profiling two live runs showed 61% of the token cost going to cache *writes*,
# most of it re-writes of a 175-280k context after the agent parked in a single
# ~10-minute wait on a background command and blew the 5-minute cache TTL.
WORKING_EFFICIENTLY = """## Working efficiently (a cost constraint, not a style note)

Everything you pull into context is re-sent on every later step of this task, and the
prompt cache expires after five minutes of inactivity — once it does, the whole context is
re-billed at write price. So:

- Do not sit idle. If you start a long-running command in the background, check on it every
  two to three minutes and do other useful work in between; never park in a single wait for
  more than four minutes.
- Read narrowly. Search with grep/glob before opening a file, and read the line ranges you
  need rather than whole files. Never re-read a file you have already read in this session.
- Keep command output small. Filter long output through head/tail/grep instead of printing
  it whole.
"""

VERDICT_SCHEMA = """{
  "verdict": "clean | findings",
  "summary": "1-2 sentence overall assessment",
  "findings": [
    {"severity": "critical | major | minor", "file": "path/to/file.py",
     "line": 120, "title": "short issue title", "detail": "what is wrong and how to fix it"}
  ]
}"""


def parse_verdict(text: str) -> Verdict:
    data = find_json_object(text, "verdict")
    if data is None:
        raise VerdictError("no JSON object in the reviewer message")
    verdict = data.get("verdict")
    if verdict not in ("clean", "findings"):
        raise VerdictError(f"unknown verdict value: {verdict!r}")
    summary = data.get("summary") or ""
    findings: list[Finding] = []
    if verdict == "findings":
        for raw in data.get("findings") or []:
            if not isinstance(raw, dict) or not raw.get("file") or not raw.get("title"):
                raise VerdictError(f"finding without file/title: {raw!r}")
            line = raw.get("line")
            findings.append(Finding(
                severity=raw.get("severity") or "major",
                file=str(raw["file"]),
                title=str(raw["title"]),
                detail=str(raw.get("detail") or ""),
                line=int(line) if isinstance(line, int) else None,
            ))
    return Verdict(verdict=verdict, summary=str(summary), findings=findings)


def newly_fixed(pending: list[Finding], current: list[Finding]) -> list[Finding]:
    """Pending findings that no longer show up in the current verdict."""
    still = {(f.file, f.title) for f in current}
    return [f for f in pending if (f.file, f.title) not in still]


def report_dict(summary: str, fixed: list[Finding], remaining: list[Finding]) -> dict:
    return {"summary": summary,
            "fixed": [asdict(f) for f in fixed],
            "remaining": [asdict(f) for f in remaining]}


def build_review_prompt(spec_path: str, plan_path: str, head_branch: str) -> str:
    return (
        "You are an independent code reviewer.\n"
        "This is a fresh session: you did not write this code, you have no memory "
        "of it being written, and there is no earlier conversation to recall. "
        "Everything you need is in this prompt and in the repository on disk.\n\n"
        "The repository is checked out in the current working directory, on the "
        "pull request branch. The work under review is exactly what sits on top "
        "of the imported PR branch — start by looking at it:\n"
        f"  git diff --stat origin/{head_branch}..HEAD   # which files changed\n"
        f"  git log --oneline origin/{head_branch}..HEAD # the commits\n"
        f"  git diff origin/{head_branch}..HEAD          # the change itself\n"
        "Uncommitted leftovers count as part of the work under review — "
        "`git status --short` shows them.\n"
        "Review that diff, not the repository as a whole. Open a whole file only "
        "when the diff alone cannot tell you whether the change is correct, and "
        "then read just the part you need.\n\n"
        f"Specification: {spec_path}\n"
        f"Plan: {plan_path}\n"
        "Read the sections of those two documents that the diff touches, to learn "
        "what the change was supposed to do; do not read them cover to cover if "
        "they are long.\n\n"
        "Check for: correctness bugs, security issues, deviations from the spec "
        "and plan, test quality and coverage, style problems. Report ALL findings "
        "regardless of severity.\n"
        "Do NOT modify, commit or push anything — you only review.\n\n"
        + WORKING_EFFICIENTLY +
        "\nYour FINAL message must be a single JSON object and nothing else, "
        "matching exactly this schema:\n"
        f"{VERDICT_SCHEMA}\n"
        "Keep each \"detail\" to one or two sentences saying what is wrong and how "
        "to fix it: point at the file and line, do not quote the code back.\n"
        'If the work is acceptable, return {"verdict": "clean", '
        '"summary": "<why it is clean>", "findings": []}.'
    )


def build_fix_prompt(verdict: Verdict, test_cmd: str | None,
                     head_branch: str | None = None,
                     spec_path: str | None = None,
                     plan_path: str | None = None) -> str:
    findings_json = json.dumps([asdict(f) for f in verdict.findings],
                               ensure_ascii=False, indent=2)
    test_line = (f"After fixing, run the tests with `{test_cmd}` — they must pass.\n"
                 if test_cmd else "")
    if head_branch:
        orient = (
            f"  git diff --stat origin/{head_branch}..HEAD  # the work that was reviewed\n"
            f"  git diff origin/{head_branch}..HEAD         # the change itself, "
            "when a finding needs its context\n"
            "Uncommitted leftovers count too — `git status --short` shows them.\n")
    else:
        orient = ("  git status --short\n"
                  "  git log --oneline -5\n")
    # A "deviates from the spec" finding is unactionable without the document it
    # deviates from, and the fixer has no session to have read it in.
    doc_lines = [f"Specification: {p}" for p in (spec_path,) if p]
    doc_lines += [f"Plan: {p}" for p in (plan_path,) if p]
    docs = ("\n".join(doc_lines) + "\n"
            "A finding that says the work deviates from those documents is judged "
            "against them: read the section it points at — not the whole "
            "document — before changing anything.\n\n") if doc_lines else ""
    return (
        "An independent code review of this repository found the issues listed "
        "below, and you are fixing them.\n"
        "This is a fresh session: you did not write the reviewed code, you have no "
        "memory of it, and there is no earlier conversation to recall. The findings "
        "below are the complete report — nothing else was raised, and there is "
        "nothing else to look up.\n\n"
        "The repository is checked out in the current working directory, on the "
        "branch that carries the work. Orient yourself cheaply:\n"
        + orient +
        "Then open only the files a finding names, around the lines it names.\n\n"
        + docs +
        "Fix ALL of the findings listed below.\n"
        "Make a git commit after the fixes. Do not git push. Do not switch branches.\n"
        + test_line + "\n"
        "Findings (JSON):\n" + findings_json + "\n\n"
        + WORKING_EFFICIENTLY +
        "\nFinish with a short summary of what you changed: one line per finding, "
        "no code quoted back."
    )


def build_revise_prompt(feedback: str, test_cmd: str | None,
                        head_branch: str | None = None,
                        spec_path: str | None = None,
                        plan_path: str | None = None,
                        resumed: bool = False) -> str:
    """The prompt for human feedback left on a run paused in awaiting_approval.

    `resumed=True` means the task will be submitted with `continue`, and the
    session it lands in is the executor's own — the agent still remembers
    writing this code, so the prompt is one paragraph. That is only true when
    neither review nor e2e ran: sandboxd can resume *the most recent* session
    and nothing else (`Continue *bool` -> `claude --continue`), so any later
    stage puts its own session in front of the executor's.

    `resumed=False` is the general case, and then the prompt has to carry what
    the executor's session would have carried: where the work is, which
    documents define it, and how to run the tests.
    """
    test_line = (f"After the changes, run the tests with `{test_cmd}` — they must pass.\n"
                 if test_cmd else "")
    if resumed:
        return (
            "A human reviewer looked at the staged result of your work and left "
            "feedback:\n\n" + feedback + "\n\n"
            "Address it in this repository. Make a git commit. Do not git push. "
            "Do not switch branches.\n"
            + test_line +
            "The efficiency rules from the start of this session still apply.\n"
            "Finish with a short summary of what you changed."
        )
    if head_branch:
        orient = (
            f"  git diff --stat origin/{head_branch}..HEAD  # the work the feedback is about\n"
            f"  git diff origin/{head_branch}..HEAD         # the change itself, "
            "where the feedback needs its context\n"
            "Uncommitted leftovers count too — `git status --short` shows them.\n")
    else:
        orient = ("  git status --short\n"
                  "  git log --oneline -5\n")
    doc_lines = [f"Specification: {p}" for p in (spec_path,) if p]
    doc_lines += [f"Plan: {p}" for p in (plan_path,) if p]
    docs = ("\n".join(doc_lines) + "\n"
            "Those documents define what this work was supposed to do. Read the "
            "section the feedback touches — not the whole document — when the "
            "feedback is about behaviour rather than about the code itself.\n\n"
            ) if doc_lines else ""
    return (
        "A human reviewer looked at the staged result of this work and left the "
        "feedback below, and you are addressing it.\n"
        "This is a fresh session: you did not write this code, you have no memory "
        "of it being written, and there is no earlier conversation to recall. The "
        "feedback below is the whole of what was asked — nothing else was raised, "
        "and there is nothing else to look up.\n\n"
        "The repository is checked out in the current working directory, on the "
        "branch that carries the work. Orient yourself cheaply:\n"
        + orient +
        "Then open only the files the feedback concerns.\n\n"
        + docs +
        "Feedback from the reviewer:\n\n" + feedback + "\n\n"
        "Address it in this repository. Make a git commit. Do not git push. "
        "Do not switch branches.\n"
        + test_line + "\n"
        + WORKING_EFFICIENTLY +
        "\nFinish with a short summary of what you changed."
    )


_VERDICT_LINES = {"clean": "✅ clean",
                  "escalated": "⚠️ findings remain",
                  "skipped": "⛔ review skipped"}


def _fmt_finding(f: dict) -> str:
    loc = f["file"] + (f":{f['line']}" if f.get("line") else "")
    return f"- **[{f.get('severity', 'major')}]** `{loc}` — {f['title']}"


def format_review_comment(status: str, iterations: int, report: dict) -> str:
    lines = ["**🤖 loop-orchestrator — review (Fable 5)**", "",
             f"**Verdict: {_VERDICT_LINES[status]}** ({iterations} fix iteration(s))"]
    if report.get("summary"):
        lines += ["", report["summary"]]
    for key, title in (("fixed", "Fixed in the fix cycle"), ("remaining", "Remaining")):
        items = report.get(key) or []
        if items:
            lines += ["", f"**{title} ({len(items)}):**"]
            lines += [_fmt_finding(f) for f in items]
    return "\n".join(lines)
