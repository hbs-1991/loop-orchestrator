"""e2e.py protocol: verdict parsing, prompts, video selection, zip extraction."""
import io
import json
import zipfile

import pytest

from loop_orchestrator.e2e import (
    E2E_DIR,
    MAX_VIDEO_BYTES,
    MAX_VIDEOS,
    E2ETest,
    E2EVerdict,
    E2EVerdictError,
    build_e2e_fix_prompt,
    build_e2e_prompt,
    e2e_report_dict,
    extract_from_zip,
    format_e2e_comment,
    parse_e2e_verdict,
    select_video_paths,
)

PASSED_JSON = json.dumps({
    "verdict": "passed", "summary": "all good",
    "tests": [{"title": "signup flow", "status": "passed", "video": ".loop/e2e/main.mp4"}],
    "main_video": ".loop/e2e/main.mp4"})


def test_parse_passed_verdict():
    v = parse_e2e_verdict(f"some preamble\n{PASSED_JSON}")
    assert v.verdict == "passed"
    assert v.main_video == ".loop/e2e/main.mp4"
    assert v.tests[0].title == "signup flow"
    assert v.tests[0].status == "passed"


def test_parse_failed_verdict():
    v = parse_e2e_verdict(json.dumps({
        "verdict": "failed", "summary": "broken",
        "tests": [{"title": "login", "status": "failed", "video": ".loop/e2e/fail-1.mp4"},
                  {"title": "logout", "status": "passed", "video": None}],
        "main_video": None}))
    assert v.verdict == "failed"
    assert [t.status for t in v.tests] == ["failed", "passed"]


def test_parse_rejects_no_json():
    with pytest.raises(E2EVerdictError):
        parse_e2e_verdict("no json here")


def test_parse_rejects_bad_verdict_value():
    with pytest.raises(E2EVerdictError):
        parse_e2e_verdict('{"verdict": "maybe", "summary": "", "tests": []}')


def test_parse_rejects_test_without_title():
    with pytest.raises(E2EVerdictError):
        parse_e2e_verdict('{"verdict": "failed", "tests": [{"status": "failed"}]}')


def test_prompt_in_sandbox_mode():
    p = build_e2e_prompt("docs/specs/x-design.md", "npm run dev",
                         {"VITE_API_URL": "http://localhost:8000"})
    assert "docs/specs/x-design.md" in p
    assert "npm run dev" in p
    assert "VITE_API_URL=http://localhost:8000" in p
    assert ".loop/e2e" in p
    assert ".gitignore" in p
    assert "playwright-cli" in p
    assert "Do not git push" in p


def test_prompt_staging_mode():
    p = build_e2e_prompt("docs/specs/x-design.md", None, {"E2E_BASE_URL": "https://stage.app"})
    assert "already deployed" in p
    assert "E2E_BASE_URL=https://stage.app" in p


def test_fix_prompt_lists_only_failing():
    v = E2EVerdict(verdict="failed", summary="s", main_video=None, tests=[
        E2ETest(title="login", status="failed", video=None),
        E2ETest(title="logout", status="passed", video=None)])
    p = build_e2e_fix_prompt(v, "npm test")
    assert "login" in p
    assert "logout" not in p
    assert "npm test" in p
    assert "Do not weaken" in p


def test_report_dict_roundtrip():
    v = parse_e2e_verdict(PASSED_JSON)
    d = e2e_report_dict("all good", v)
    assert d["main_video"] == ".loop/e2e/main.mp4"
    assert d["tests"][0]["title"] == "signup flow"
    assert e2e_report_dict("nothing", None) == {"summary": "nothing", "tests": [],
                                                "main_video": None}


def test_select_videos_passed():
    report = {"summary": "s", "main_video": ".loop/e2e/main.mp4",
              "tests": [{"title": "t", "status": "passed", "video": ".loop/e2e/main.mp4"}]}
    assert select_video_paths("passed", report) == [".loop/e2e/main.mp4"]


def test_select_videos_escalated_caps_at_max():
    tests = [{"title": f"t{i}", "status": "failed", "video": f".loop/e2e/fail-{i}.mp4"}
             for i in range(5)]
    report = {"summary": "s", "main_video": None, "tests": tests}
    got = select_video_paths("escalated", report)
    assert len(got) == MAX_VIDEOS
    assert got[0] == ".loop/e2e/fail-0.mp4"


def test_select_videos_escalated_dedupes_shared_video():
    tests = [{"title": "a", "status": "failed", "video": ".loop/e2e/fail-1.mp4"},
             {"title": "b", "status": "failed", "video": ".loop/e2e/fail-1.mp4"},
             {"title": "c", "status": "failed", "video": ".loop/e2e/fail-2.mp4"}]
    report = {"summary": "s", "main_video": None, "tests": tests}
    assert select_video_paths("escalated", report) == [
        ".loop/e2e/fail-1.mp4", ".loop/e2e/fail-2.mp4"]


def test_select_videos_skipped_is_empty():
    assert select_video_paths("skipped", {"summary": "s", "tests": [],
                                          "main_video": None}) == []


def test_extract_from_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(".loop/e2e/main.mp4", b"vid")
        zf.writestr("src/app.py", b"code")
    got = extract_from_zip(buf.getvalue(), [".loop/e2e/main.mp4", ".loop/e2e/gone.mp4"])
    assert got == {".loop/e2e/main.mp4": b"vid"}


def test_format_comment_has_table_and_verdict():
    report = {"summary": "looks solid",
              "tests": [{"title": "signup", "status": "passed", "video": None},
                        {"title": "login", "status": "failed", "video": None}],
              "main_video": None}
    c = format_e2e_comment("escalated", 2, report)
    assert "failures remain" in c
    assert "| signup |" in c
    assert "❌" in c and "✅" in c
    assert "2 fix iteration(s)" in c
