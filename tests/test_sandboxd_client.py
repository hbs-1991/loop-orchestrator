import json
import time

import httpx
import pytest
import respx

from loop_orchestrator.clients.sandboxd import SandboxdClient

SB = "http://sb:9090"


def make_client() -> SandboxdClient:
    return SandboxdClient(SB, "key1")


@respx.mock
async def test_create_app_sends_git_block():
    route = respx.post(f"{SB}/v1/apps").mock(
        return_value=httpx.Response(200, json={"id": "app1", "name": "n"}))
    app_id = await make_client().create_app(
        "loop-r-pr5-r7", "https://github.com/o/r.git", "feat/x", "cred1", preset="node")
    assert app_id == "app1"
    body = json.loads(route.calls[0].request.content)
    assert body["git"] == {"repo_url": "https://github.com/o/r.git",
                           "branch": "feat/x", "credential_id": "cred1"}
    assert body["runtime_preset"] == "node"
    assert route.calls[0].request.headers["authorization"] == "Bearer key1"


@respx.mock
async def test_delete_app_ignores_404_and_none():
    respx.delete(f"{SB}/v1/apps/gone").mock(return_value=httpx.Response(404))
    c = make_client()
    await c.delete_app("gone")
    await c.delete_app(None)  # no request, no error


@respx.mock
async def test_create_sandbox_adopts_existing_on_409():
    # Live repro (run #23): the first POST created the sandbox but the reply
    # was lost to a transport timeout (slow first seed of a fresh repo);
    # with_retries re-POSTed and got 409 — the client must adopt the sandbox
    # that already exists instead of failing the run.
    respx.post(f"{SB}/v1/apps/app1/sandbox").mock(
        return_value=httpx.Response(409, json={"error": "sandbox exists"}))
    respx.get(f"{SB}/v1/apps/app1").mock(
        return_value=httpx.Response(200, json={
            "id": "app1", "current_sandbox_id": "sb-existing"}))
    respx.get(f"{SB}/v1/sandboxes/sb-existing").mock(
        return_value=httpx.Response(200, json={"id": "sb-existing",
                                               "status": "running"}))
    c = make_client()
    assert await c.create_sandbox("app1") == "sb-existing"


@respx.mock
async def test_create_sandbox_refuses_to_adopt_a_sandbox_in_error():
    # Run #57: the workspace seed failed (the sandbox image had been pruned off
    # the host), leaving the row in `error`; with_retries re-POSTed the 500 and
    # got 409. Adopting that sandbox handed the run one that answered 409 to
    # every task for three hours — failing here is the cheaper answer.
    respx.post(f"{SB}/v1/apps/app1/sandbox").mock(
        return_value=httpx.Response(409, json={"error": "sandbox exists"}))
    respx.get(f"{SB}/v1/apps/app1").mock(
        return_value=httpx.Response(200, json={
            "id": "app1", "current_sandbox_id": "sb-dead"}))
    respx.get(f"{SB}/v1/sandboxes/sb-dead").mock(
        return_value=httpx.Response(200, json={"id": "sb-dead", "status": "error"}))
    c = make_client()
    with pytest.raises(httpx.HTTPStatusError):
        await c.create_sandbox("app1")


@respx.mock
async def test_stop_sandbox_is_best_effort():
    respx.post(f"{SB}/v1/sandboxes/sb1/stop").mock(
        return_value=httpx.Response(200, json={"id": "sb1", "status": "stopped"}))
    # 409 = "a task is in progress" — sandboxd guards a working agent for us.
    respx.post(f"{SB}/v1/sandboxes/sb2/stop").mock(
        return_value=httpx.Response(409, json={"error": "task_in_progress"}))
    c = make_client()
    assert await c.stop_sandbox("sb1") is True
    assert await c.stop_sandbox("sb2") is False


@respx.mock
async def test_validate_manifest_reports_errors_and_tolerates_an_outage():
    route = respx.post(f"{SB}/v1/runtime/manifest/validate").mock(
        return_value=httpx.Response(200, json={"errors": ["web.port is missing"],
                                               "warnings": []}))
    c = make_client()
    assert await c.validate_manifest("version: 1\n") == ["web.port is missing"]
    assert json.loads(route.calls[0].request.content)["manifest"] == "version: 1\n"
    # Unreachable validator: a pre-flight check, not a gate.
    route.mock(return_value=httpx.Response(500))
    assert await c.validate_manifest("version: 1\n") == []


@respx.mock
async def test_secret_sandbox_task_flow():
    sec = respx.post(f"{SB}/v1/apps/app1/config").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{SB}/v1/apps/app1/sandbox").mock(
        return_value=httpx.Response(200, json={"id": "sb1"}))
    task = respx.post(f"{SB}/v1/sandboxes/sb1/tasks").mock(
        return_value=httpx.Response(200, json={"id": "t1", "status": "running"}))
    respx.get(f"{SB}/v1/sandboxes/sb1/tasks/t1").mock(
        return_value=httpx.Response(200, json={"id": "t1", "status": "succeeded",
                                               "agent_message": "done"}))
    c = make_client()
    await c.set_app_secret("app1", "DB_URL", "postgres://x")
    assert json.loads(sec.calls[0].request.content) == {
        "key": "DB_URL", "value": "postgres://x", "sensitive": True, "access_policy": "both"}
    assert await c.create_sandbox("app1") == "sb1"
    tid = await c.submit_task("sb1", "do it", timeout_s=600)
    assert tid == "t1"
    body = json.loads(task.calls[0].request.content)
    assert body == {"prompt": "do it", "agent": "claude-code", "timeout_s": 600}
    got = await c.get_task("sb1", "t1")
    assert got["status"] == "succeeded"


@respx.mock
async def test_submit_task_continue():
    route = respx.post(f"{SB}/v1/sandboxes/sb1/tasks").mock(
        return_value=httpx.Response(200, json={"id": "t2"}))
    await make_client().submit_task("sb1", "continue", timeout_s=60, continue_session=True)
    assert json.loads(route.calls[0].request.content)["continue"] is True


@respx.mock
async def test_submit_task_forces_a_fresh_session():
    # False must reach the wire: sandboxd's `continue` is tri-state and its
    # default is "resume the previous session", so omitting the field is the
    # opposite of a fresh start.
    route = respx.post(f"{SB}/v1/sandboxes/sb1/tasks").mock(
        return_value=httpx.Response(200, json={"id": "t3"}))
    await make_client().submit_task("sb1", "review", timeout_s=60, continue_session=False)
    assert json.loads(route.calls[0].request.content)["continue"] is False


@respx.mock
async def test_submit_task_without_continue_omits_the_field():
    route = respx.post(f"{SB}/v1/sandboxes/sb1/tasks").mock(
        return_value=httpx.Response(200, json={"id": "t4"}))
    await make_client().submit_task("sb1", "do it", timeout_s=60)
    assert "continue" not in json.loads(route.calls[0].request.content)


@respx.mock
async def test_submit_task_with_model():
    captured = {}

    def cb(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "t1"})

    respx.post(f"{SB}/v1/sandboxes/sb1/tasks").mock(side_effect=cb)
    c = make_client()
    tid = await c.submit_task("sb1", "review this", timeout_s=60, model="claude-fable-5")
    assert tid == "t1"
    assert captured["model"] == "claude-fable-5"
    assert captured["agent"] == "claude-code"
    await c.aclose()


@respx.mock
async def test_submit_task_without_model_omits_field():
    captured = {}

    def cb(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "t2"})

    respx.post(f"{SB}/v1/sandboxes/sb1/tasks").mock(side_effect=cb)
    c = make_client()
    await c.submit_task("sb1", "do work", timeout_s=60)
    assert "model" not in captured
    await c.aclose()


@respx.mock
async def test_git_ops_and_cancel_swallow():
    respx.post(f"{SB}/v1/apps/app1/git/commit").mock(
        return_value=httpx.Response(200, json={"committed": False, "reason": "no_changes"}))
    respx.post(f"{SB}/v1/apps/app1/git/push").mock(
        return_value=httpx.Response(200, json={"pushed": True, "branch": "loop/run-7", "commits": 3}))
    respx.post(f"{SB}/v1/sandboxes/sb1/tasks/t1/cancel").mock(return_value=httpx.Response(500))
    c = make_client()
    assert (await c.git_commit("app1", "msg"))["reason"] == "no_changes"
    assert (await c.git_push("app1", "loop/run-7"))["pushed"] is True
    await c.cancel_task("sb1", "t1")  # a 500 does not blow up


@respx.mock
async def test_list_files():
    respx.get(f"{SB}/v1/sandboxes/sb1/files").respond(200, json={
        "path": ".loop/e2e", "recursive": False,
        "entries": [{"path": ".loop/e2e/main.mp4", "type": "file", "size": 1024}]})
    c = make_client()
    entries = await c.list_files("sb1", ".loop/e2e")
    assert entries == [{"path": ".loop/e2e/main.mp4", "type": "file", "size": 1024}]
    await c.aclose()


@respx.mock
async def test_list_files_missing_dir_is_empty():
    respx.get(f"{SB}/v1/sandboxes/sb1/files").respond(404, json={"error": {}})
    c = make_client()
    assert await c.list_files("sb1", ".loop/e2e") == []
    await c.aclose()


@respx.mock
async def test_read_file_bytes():
    respx.get(f"{SB}/v1/sandboxes/sb1/files/content").respond(200, content=b"\x00video")
    c = make_client()
    assert await c.read_file("sb1", ".loop/e2e/main.mp4") == b"\x00video"
    await c.aclose()


@respx.mock
async def test_read_file_missing_or_too_big_is_none():
    respx.get(f"{SB}/v1/sandboxes/sb1/files/content").respond(400, json={"error": {}})
    c = make_client()
    assert await c.read_file("sb1", ".loop/e2e/huge.mp4") is None
    await c.aclose()


@respx.mock
async def test_get_sandbox():
    respx.get(f"{SB}/v1/sandboxes/sb1").respond(
        200, json={"id": "sb1", "preview": {"url": "https://s-sb1-3000.preview.x", "port": 3000}})
    c = make_client()
    info = await c.get_sandbox("sb1")
    assert info["preview"]["url"] == "https://s-sb1-3000.preview.x"
    await c.aclose()


@respx.mock
async def test_exec_cmd_uses_the_unprefixed_route_and_returns_the_exit_code():
    # The internal surface, like keepalive: /sandbox/{id}/exec, no /v1.
    route = respx.post(f"{SB}/sandbox/sb1/exec").respond(
        200, json={"stdout": "", "stderr": "", "exit_code": 1})
    c = make_client()
    res = await c.exec_cmd("sb1", ["python3", "-c", "pass"])
    # A non-zero exit is an answer, not a failure — the caller decides.
    assert res["exit_code"] == 1
    assert json.loads(route.calls[0].request.content)["cmd"] == ["python3", "-c", "pass"]
    await c.aclose()


@respx.mock
async def test_export_zip():
    respx.get(f"{SB}/v1/sandboxes/sb1/export").respond(200, content=b"PK\x03\x04zipbytes")
    c = make_client()
    assert (await c.export_zip("sb1")).startswith(b"PK")
    await c.aclose()


@respx.mock
async def test_list_tasks():
    respx.get(f"{SB}/v1/sandboxes/sb1/tasks").respond(
        200, json={"tasks": [{"id": "t1", "status": "running"}]})
    client = make_client()
    tasks = await client.list_tasks("sb1")
    assert tasks == [{"id": "t1", "status": "running"}]


@respx.mock
async def test_keepalive_posts_a_future_deadline():
    seen = {}

    def capture(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "sb1", "keepalive_until": 1})

    # Note the missing /v1: keepalive lives on sandboxd's internal surface.
    respx.post(f"{SB}/sandbox/sb1/keepalive").mock(side_effect=capture)
    c = make_client()
    await c.keepalive("sb1", 30)
    assert seen["until"] > time.time() + 29 * 60
    await c.aclose()


@respx.mock
async def test_sanitize_git_config_unsets_only_what_is_set():
    # `pnpm install` in a husky repo writes core.hooksPath, and sandboxd's
    # pre-push audit then refuses the push as unsafe_repo_config (run #45).
    calls: list[list[str]] = []

    def capture(request):
        cmd = json.loads(request.content)["cmd"]
        calls.append(cmd)
        # git exits 5 for a key that is not set; 0 means it removed one.
        code = 0 if cmd[-1] == "core.hooksPath" else 5
        return httpx.Response(200, json={"stdout": "", "stderr": "", "exit_code": code})

    respx.post(f"{SB}/sandbox/sb1/exec").mock(side_effect=capture)
    c = make_client()
    assert await c.sanitize_git_config("sb1") == ["core.hooksPath"]
    assert [cmd[-1] for cmd in calls] == list(c.UNSAFE_GIT_KEYS)
    assert calls[0][:5] == ["git", "-C", "/home/sandbox/workspace/app", "config", "--local"]
    await c.aclose()


@respx.mock
async def test_sanitize_git_config_survives_a_sandboxd_without_exec():
    respx.post(f"{SB}/sandbox/sb1/exec").respond(404)
    c = make_client()
    assert await c.sanitize_git_config("sb1") == []
    await c.aclose()


@respx.mock
async def test_keepalive_swallows_a_sandboxd_without_the_route():
    # An older sandboxd answers 404; losing a keepalive must not fail the run.
    respx.post(f"{SB}/sandbox/sb1/keepalive").respond(404)
    c = make_client()
    await c.keepalive("sb1", 30)
    await c.aclose()
