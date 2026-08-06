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
        "You are an independent code reviewer for this repository.\n"
        f"Specification: {spec_path}\n"
        f"Plan: {plan_path}\n\n"
        "Read both documents first. Then review ONLY the work done on top of the "
        f"imported PR branch: inspect `git log origin/{head_branch}..HEAD` and "
        f"`git diff origin/{head_branch}..HEAD`, plus any uncommitted changes "
        "shown by `git status`.\n"
        "Check for: correctness bugs, security issues, deviations from the spec "
        "and plan, test quality and coverage, style problems. Report ALL findings "
        "regardless of severity.\n"
        "Do NOT modify, commit or push anything — you only review.\n\n"
        "Your FINAL message must be a single JSON object and nothing else, "
        "matching exactly this schema:\n"
        f"{VERDICT_SCHEMA}\n"
        'If the work is acceptable, return {"verdict": "clean", '
        '"summary": "<why it is clean>", "findings": []}.'
    )


def build_fix_prompt(verdict: Verdict, test_cmd: str | None) -> str:
    findings_json = json.dumps([asdict(f) for f in verdict.findings],
                               ensure_ascii=False, indent=2)
    test_line = (f"After fixing, run the tests with `{test_cmd}` — they must pass.\n"
                 if test_cmd else "")
    return (
        "An independent code review found issues in the work done in this repository.\n"
        "Fix ALL of the findings listed below.\n"
        "Make a git commit after the fixes. Do not git push. Do not switch branches.\n"
        + test_line +
        "Findings (JSON):\n" + findings_json + "\n"
        "Finish with a short summary of what you changed."
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
