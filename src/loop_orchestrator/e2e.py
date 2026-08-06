"""E2E verdict protocol: prompts, JSON verdict parsing, video selection, PR comment."""
import io
import json
import zipfile
from dataclasses import asdict, dataclass, field

from .jsonextract import find_json_object
from .secrets import source_hint

E2E_DIR = ".loop/e2e"
MAX_VIDEOS = 3
MAX_VIDEO_BYTES = 45 * 1024 * 1024  # Telegram bot uploads are capped at 50 MB


class E2EVerdictError(Exception):
    pass


@dataclass
class E2ETest:
    title: str
    status: str  # "passed" | "failed"
    video: str | None = None


@dataclass
class E2EVerdict:
    verdict: str  # "passed" | "failed"
    summary: str = ""
    tests: list[E2ETest] = field(default_factory=list)
    main_video: str | None = None


E2E_VERDICT_SCHEMA = """{
  "verdict": "passed | failed",
  "summary": "1-2 sentence overall assessment",
  "tests": [
    {"title": "scenario name", "status": "passed | failed",
     "video": ".loop/e2e/<file> or null"}
  ],
  "main_video": ".loop/e2e/main.mp4 or null"
}"""


def parse_e2e_verdict(text: str) -> E2EVerdict:
    data = find_json_object(text, "verdict")
    if data is None:
        raise E2EVerdictError("no JSON object in the e2e agent message")
    verdict = data.get("verdict")
    if verdict not in ("passed", "failed"):
        raise E2EVerdictError(f"unknown e2e verdict value: {verdict!r}")
    tests: list[E2ETest] = []
    for raw in data.get("tests") or []:
        if not isinstance(raw, dict) or not raw.get("title"):
            raise E2EVerdictError(f"test entry without title: {raw!r}")
        status = raw.get("status")
        if status not in ("passed", "failed"):
            raise E2EVerdictError(f"unknown test status: {status!r}")
        video = raw.get("video")
        tests.append(E2ETest(title=str(raw["title"]), status=status,
                             video=str(video) if video else None))
    main_video = data.get("main_video")
    return E2EVerdict(verdict=verdict, summary=str(data.get("summary") or ""),
                      tests=tests, main_video=str(main_video) if main_video else None)


def build_e2e_prompt(spec_path: str, run_cmd: str | None, e2e_env: dict[str, str],
                     secrets: dict[str, str] | None = None) -> str:
    env_lines = "\n".join(f"  {k}={v}" for k, v in e2e_env.items()) or "  (none)"
    if run_cmd:
        env_block = (
            "Start the application yourself: export the environment variables "
            f"below, run `{run_cmd}` in the background, and wait until it is ready.\n"
            f"Environment variables:\n{env_lines}\n")
    else:
        env_block = (
            "The application under test is already deployed externally; use the "
            f"environment variables below to locate it.\nEnvironment variables:\n{env_lines}\n")
    return (
        "You are an end-to-end tester for this repository. Verify, as a real user "
        "would, that the feature described in the specification works in the "
        "running application.\n"
        f"Specification: {spec_path}\n\n"
        + env_block + source_hint(secrets or {}) +
        "\nUse the playwright-cli skill for all browser work: explore the feature "
        "interactively first, then write Playwright test scenarios.\n\n"
        "Requirements:\n"
        "1. Write Playwright e2e tests covering the feature's main user scenario "
        "and its critical paths, guided by the specification. If the repository "
        "already has a Playwright setup, follow its structure; otherwise create "
        "`e2e/` with a `playwright.config.*`. Enable video recording; use headless "
        "chromium.\n"
        "2. Run the tests.\n"
        f"3. Copy the selected artifacts into `{E2E_DIR}/`:\n"
        f"   - `{E2E_DIR}/main.mp4` — a video of the main scenario working end-to-end "
        "(convert webm to mp4 with ffmpeg; keep it short, ~60-90s, 1280x720)\n"
        f"   - `{E2E_DIR}/fail-<n>.<ext>` — videos of failing tests (at most "
        f"{MAX_VIDEOS})\n"
        "4. Make sure `.gitignore` contains a `.loop/` entry (add one if missing).\n"
        "5. Commit the test scenarios and the .gitignore change. Do not commit "
        "`.loop/`. Do not git push. Do not switch branches.\n"
        "For an API-only feature without a UI, write Playwright request-based tests "
        'instead; then there are no videos and "main_video" must be null.\n\n'
        "Your FINAL message must be a single JSON object and nothing else, "
        "matching exactly this schema:\n"
        f"{E2E_VERDICT_SCHEMA}"
    )


def build_e2e_fix_prompt(verdict: E2EVerdict, test_cmd: str | None) -> str:
    failing = [asdict(t) for t in verdict.tests if t.status == "failed"]
    test_line = (f"After fixing, run the unit tests with `{test_cmd}` — they must pass.\n"
                 if test_cmd else "")
    return (
        "End-to-end tests found that the feature does not fully work as specified.\n"
        "Fix the application code so the scenarios below pass.\n"
        "Do not weaken or delete tests to make them pass; only change a test if it "
        "contradicts the specification.\n"
        "Make a git commit after the fixes. Do not git push. Do not switch branches.\n"
        + test_line +
        "Failing scenarios (JSON):\n"
        + json.dumps(failing, ensure_ascii=False, indent=2) + "\n"
        "Finish with a short summary of what you changed."
    )


def e2e_report_dict(summary: str, verdict: E2EVerdict | None) -> dict:
    if verdict is None:
        return {"summary": summary, "tests": [], "main_video": None}
    return {"summary": summary,
            "tests": [asdict(t) for t in verdict.tests],
            "main_video": verdict.main_video}


def select_video_paths(status: str, report: dict) -> list[str]:
    if status == "passed":
        return [report["main_video"]] if report.get("main_video") else []
    if status == "escalated":
        vids = [t["video"] for t in report.get("tests") or []
                if t.get("status") == "failed" and t.get("video")]
        # Several failing tests may share one video file — dedupe before capping.
        return list(dict.fromkeys(vids))[:MAX_VIDEOS]
    return []


def extract_from_zip(archive: bytes, paths: list[str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = set(zf.namelist())
        for p in paths:
            if p in names:
                out[p] = zf.read(p)
    return out


_E2E_LINES = {"passed": "✅ passed",
              "escalated": "⚠️ failures remain",
              "skipped": "⛔ e2e skipped"}


def format_e2e_comment(status: str, iterations: int, report: dict) -> str:
    lines = ["**🤖 loop-orchestrator — e2e (playwright-cli)**", "",
             f"**Verdict: {_E2E_LINES[status]}** ({iterations} fix iteration(s))"]
    if report.get("summary"):
        lines += ["", report["summary"]]
    tests = report.get("tests") or []
    if tests:
        lines += ["", "| Scenario | Status |", "|---|---|"]
        lines += [f"| {t['title']} | "
                  f"{'✅' if t['status'] == 'passed' else '❌'} {t['status']} |"
                  for t in tests]
    return "\n".join(lines)
