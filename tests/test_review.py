import pytest

from loop_orchestrator.review import (
    WORKING_EFFICIENTLY,
    Finding,
    Verdict,
    VerdictError,
    build_fix_prompt,
    build_review_prompt,
    build_revise_prompt,
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


def test_review_prompt_is_self_contained_for_a_cold_session():
    # The reviewer starts in a brand-new Claude session (sandboxd `continue: false`),
    # so the prompt must carry the repo, the diff range and the documents itself.
    rp = build_review_prompt("docs/s.md", "docs/p.md", "feat/x")
    assert "fresh session" in rp and "no earlier conversation" in rp
    assert "git diff origin/feat/x..HEAD" in rp
    assert "git status --short" in rp
    assert "docs/s.md" in rp and "docs/p.md" in rp
    assert "do not quote the code back" in rp


def test_fix_prompt_is_self_contained_for_a_cold_session():
    fp = build_fix_prompt(parse_verdict(FINDINGS), None,
                          "feat/x", "docs/s.md", "docs/p.md")
    assert "fresh session" in fp and "no earlier conversation" in fp
    assert "complete report" in fp
    # The findings themselves are the whole input, and they carry their locations.
    assert "app/api.py" in fp
    # The reviewed diff and the documents a "deviates from the spec" finding
    # refers to — neither is recallable from a session that does not exist.
    assert "git diff origin/feat/x..HEAD" in fp
    assert "docs/s.md" in fp and "docs/p.md" in fp


def test_fix_prompt_degrades_without_the_optional_context():
    fp = build_fix_prompt(parse_verdict(FINDINGS), None)
    assert "git log --oneline -5" in fp
    assert "Specification:" not in fp and "Plan:" not in fp
    assert "fresh session" in fp


def test_revise_prompt_is_self_contained_for_a_cold_session():
    rp = build_revise_prompt("make the button blue", "pytest -q",
                             "feat/x", "docs/s.md", "docs/p.md")
    assert "fresh session" in rp and "no earlier conversation" in rp
    assert "make the button blue" in rp
    assert "git diff origin/feat/x..HEAD" in rp
    assert "docs/s.md" in rp and "docs/p.md" in rp
    assert "pytest -q" in rp and "Do not git push" in rp


def test_revise_prompt_degrades_without_the_optional_context():
    rp = build_revise_prompt("make the button blue", None)
    assert "git log --oneline -5" in rp
    assert "Specification:" not in rp and "pytest -q" not in rp


def test_resumed_revise_prompt_does_not_restate_the_session():
    # Continuing the executor's own session: it wrote the code and already
    # carries the efficiency rules, so restating either would be paid-for noise.
    rp = build_revise_prompt("make the button blue", "pytest -q", "feat/x",
                             "docs/s.md", "docs/p.md", resumed=True)
    assert "make the button blue" in rp and "pytest -q" in rp
    assert "fresh session" not in rp
    assert "git diff" not in rp and "docs/s.md" not in rp
    assert WORKING_EFFICIENTLY not in rp
    assert len(rp) < 600


def test_review_prompts_carry_the_efficiency_block():
    for p in (build_review_prompt("docs/s.md", "docs/p.md", "feat/x"),
              build_fix_prompt(parse_verdict(FINDINGS), "pytest -q"),
              build_revise_prompt("feedback", "pytest -q", "feat/x")):
        assert WORKING_EFFICIENTLY in p


def test_efficiency_block_states_the_two_rules_that_cost_money():
    assert "prompt cache expires after five minutes" in WORKING_EFFICIENTLY
    assert "never park in a single wait for" in WORKING_EFFICIENTLY
    assert "Never re-read a file you have already read" in WORKING_EFFICIENTLY


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
