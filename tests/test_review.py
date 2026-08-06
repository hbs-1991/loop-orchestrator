import pytest

from loop_orchestrator.review import (
    Finding,
    Verdict,
    VerdictError,
    build_fix_prompt,
    build_review_prompt,
    format_review_comment,
    newly_fixed,
    parse_verdict,
    report_dict,
)

CLEAN = '{"verdict": "clean", "summary": "looks good", "findings": []}'
FINDINGS = ('{"verdict": "findings", "summary": "issues found", "findings": ['
            '{"severity": "major", "file": "app/api.py", "line": 12, '
            '"title": "no timeout", "detail": "hangs on dead host"}]}')


def test_parse_clean():
    v = parse_verdict(CLEAN)
    assert v.verdict == "clean" and v.summary == "looks good" and v.findings == []


def test_parse_findings():
    v = parse_verdict(FINDINGS)
    assert v.verdict == "findings"
    f = v.findings[0]
    assert (f.severity, f.file, f.line, f.title) == ("major", "app/api.py", 12, "no timeout")


def test_parse_tolerates_fences_and_prose():
    text = "Here is my verdict:\n```json\n" + CLEAN + "\n```\nDone."
    assert parse_verdict(text).verdict == "clean"


def test_parse_clean_drops_findings():
    v = parse_verdict('{"verdict": "clean", "summary": "s", "findings": '
                      '[{"file": "a.py", "title": "left-over"}]}')
    assert v.findings == []


def test_parse_defaults_severity_and_detail():
    v = parse_verdict('{"verdict": "findings", "findings": [{"file": "a.py", "title": "t"}]}')
    f = v.findings[0]
    assert f.severity == "major" and f.detail == "" and f.line is None


def test_parse_rejects_garbage():
    for bad in ("", "no json here", '{"verdict": "maybe"}',
                '{"verdict": "findings", "findings": [{"title": "no file"}]}'):
        with pytest.raises(VerdictError):
            parse_verdict(bad)


def test_newly_fixed_by_file_and_title():
    a = Finding("major", "a.py", "bug A")
    b = Finding("minor", "b.py", "bug B")
    assert newly_fixed([a, b], [Finding("major", "b.py", "bug B")]) == [a]


def test_prompts_are_english_and_carry_context():
    rp = build_review_prompt("docs/s.md", "docs/p.md", "feat/x")
    assert "docs/s.md" in rp and "origin/feat/x..HEAD" in rp and '"verdict"' in rp
    fp = build_fix_prompt(parse_verdict(FINDINGS), "pytest -q")
    assert "no timeout" in fp and "pytest -q" in fp and "Do not git push" in fp
    fp2 = build_fix_prompt(parse_verdict(FINDINGS), None)
    assert "pytest -q" not in fp2


def test_format_review_comment():
    report = report_dict("summary line",
                         fixed=[Finding("major", "a.py", "bug A", line=3)],
                         remaining=[Finding("minor", "b.py", "bug B")])
    text = format_review_comment("escalated", 2, report)
    assert "loop-orchestrator — review (Fable 5)" in text
    assert "⚠️ findings remain" in text and "(2 fix iteration(s))" in text
    assert "Fixed in the fix cycle (1)" in text and "`a.py:3` — bug A" in text
    assert "Remaining (1)" in text and "`b.py` — bug B" in text
    clean = format_review_comment("clean", 0, report_dict("ok", [], []))
    assert "✅ clean" in clean
    skipped = format_review_comment("skipped", 0, report_dict("agent died", [], []))
    assert "⛔ review skipped" in skipped
