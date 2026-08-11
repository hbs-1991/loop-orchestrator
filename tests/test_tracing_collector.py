from loop_orchestrator.tracing.collector import TRACE_DIR, copy_script, fetch_session

from tests.conftest import FakeSandboxd

DEST = f"{TRACE_DIR}/execute.jsonl"


def test_the_copy_script_finds_the_app_itself():
    # sandboxd's Client.Exec passes neither -u nor -w, so the command lands in
    # the image's WORKDIR and has to locate the app directory on its own.
    s = copy_script(DEST)
    assert '$HOME/workspace/app' in s
    assert 'ls -t "$HOME"/.claude/projects/*/*.jsonl' in s
    assert DEST in s


def test_the_copy_lands_under_the_gitignored_loop_directory():
    # `.loop/` already carries a `.gitignore` containing `*`, so the copy cannot
    # ride a commit out of the sandbox even under `git add -A`.
    assert TRACE_DIR.startswith(".loop/")


async def test_happy_path_returns_the_file_bytes():
    sb = FakeSandboxd()
    sb.file_contents[DEST] = b'{"type":"assistant"}'
    assert await fetch_session(sb, "sb-1", "execute") == b'{"type":"assistant"}'
    assert sb.execs[0]["sandbox_id"] == "sb-1"


async def test_no_session_in_the_sandbox_returns_none():
    sb = FakeSandboxd()
    sb.exec_results = [{"stdout": "", "stderr": "no-session", "exit_code": 3}]
    assert await fetch_session(sb, "sb-1", "execute") is None


async def test_a_missing_file_after_a_successful_copy_returns_none():
    sb = FakeSandboxd()  # exec succeeds, file_contents has nothing
    assert await fetch_session(sb, "sb-1", "execute") is None


async def test_an_empty_file_is_treated_as_no_trace():
    sb = FakeSandboxd()
    sb.file_contents[DEST] = b""
    assert await fetch_session(sb, "sb-1", "execute") is None


async def test_a_dead_sandbox_never_raises():
    class Dead(FakeSandboxd):
        async def exec_cmd(self, *a, **kw):
            raise RuntimeError("sandbox is gone")

    assert await fetch_session(Dead(), "sb-1", "execute") is None


async def test_a_failing_read_never_raises():
    class BadRead(FakeSandboxd):
        async def read_file(self, *a, **kw):
            raise RuntimeError("files API said no")

    assert await fetch_session(BadRead(), "sb-1", "execute") is None


async def test_without_a_sandbox_id_nothing_is_attempted():
    sb = FakeSandboxd()
    assert await fetch_session(sb, "", "execute") is None
    assert sb.execs == []


async def test_each_stage_writes_its_own_file():
    sb = FakeSandboxd()
    sb.file_contents[f"{TRACE_DIR}/review.jsonl"] = b"x"
    assert await fetch_session(sb, "sb-1", "review") == b"x"
    assert f"{TRACE_DIR}/review.jsonl" in sb.execs[0]["cmd"][2]
