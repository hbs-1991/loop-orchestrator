import time

import httpx

from .retry import with_retries


class SandboxdError(Exception):
    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


class SandboxdClient:
    def __init__(self, base_url: str, api_key: str, client: httpx.AsyncClient | None = None):
        self._owns_http = client is None
        self._http = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120,
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _req(self, method: str, url: str, **kw) -> httpx.Response:
        async def call() -> httpx.Response:
            r = await self._http.request(method, url, **kw)
            if r.status_code >= 500:
                r.raise_for_status()
            return r
        return await with_retries(call)

    async def create_app(self, name: str, repo_url: str, branch: str,
                         credential_id: str, preset: str | None = None) -> str:
        body: dict = {
            "name": name,
            "git": {"repo_url": repo_url, "branch": branch, "credential_id": credential_id},
        }
        if preset:
            body["runtime_preset"] = preset
        r = await self._req("POST", "/v1/apps", json=body)
        r.raise_for_status()
        return r.json()["id"]

    async def delete_app(self, app_id: str | None) -> None:
        if not app_id:
            return
        r = await self._req("DELETE", f"/v1/apps/{app_id}")
        if r.status_code not in (204, 404):
            r.raise_for_status()

    async def put_file(self, sandbox_id: str, path: str, content: str) -> None:
        """Write a file into the sandbox, relative to the app directory.

        The only way to hand a secret to an agent. App config never reaches a
        sandbox at all — `v1_app_config.go` keeps values "owned by the control
        plane (not Docker env, workspace files, or task logs)" and the broker
        its access policies describe does not exist yet — and even container
        env would not help: runtimed scrubs every `*_TOKEN`/`*_PASSWORD`-shaped
        name out of the agent's environment (`cmd/runtimed/agentenv.go`).
        """
        r = await self._req("PUT", f"/v1/sandboxes/{sandbox_id}/files",
                            params={"path": path}, content=content.encode())
        r.raise_for_status()

    async def set_app_secret(self, app_id: str, key: str, value: str) -> None:
        r = await self._req("POST", f"/v1/apps/{app_id}/config", json={
            "key": key, "value": value, "sensitive": True, "access_policy": "both"})
        r.raise_for_status()

    async def create_sandbox(self, app_id: str) -> str:
        r = await self._req("POST", f"/v1/apps/{app_id}/sandbox", json={})
        if r.status_code == 409:
            # A previous attempt already created the sandbox but its reply was
            # lost to a transport error and with_retries re-POSTed (seen live:
            # the first seed of a fresh repo outlives the HTTP timeout).
            # Adopt the sandbox that exists instead of failing the run.
            app = await self._req("GET", f"/v1/apps/{app_id}")
            app.raise_for_status()
            existing = app.json().get("current_sandbox_id")
            if existing and (await self.get_sandbox(existing)).get("status") != "error":
                return existing
            # A creation that aborted mid-way leaves the sandbox row behind in
            # `error`, and the retry of that 500 lands right here. Adopting it
            # is worse than failing: it is 409 to every task from then on, so
            # run #57 spent three hours submitting into a sandbox that could
            # never run one (its image had been pruned off the host).
        r.raise_for_status()
        return r.json()["id"]

    async def get_sandbox(self, sandbox_id: str) -> dict:
        r = await self._req("GET", f"/v1/sandboxes/{sandbox_id}")
        r.raise_for_status()
        return r.json()

    async def start_sandbox(self, sandbox_id: str) -> bool:
        """Restart a sandbox the idle reaper stopped. True if it now runs.

        The reaper only stops the container; the workspace — repo, uncommitted
        work and the agent's session state — survives on its volume, so a
        stopped sandbox is recoverable rather than lost. Best-effort: the
        caller retries its real request either way.
        """
        try:
            r = await self._req("POST", f"/v1/sandboxes/{sandbox_id}/start")
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    async def stop_sandbox(self, sandbox_id: str) -> bool:
        """Stop a sandbox without destroying it. True if it is now stopped.

        The counterpart of `start_sandbox`, and the whole point of sleeping a
        paused Run: the container releases its ~3.5 GB while the workspace `.img`
        and the agent's session stay on disk. Idempotent — an already-stopped
        sandbox answers 200.

        sandboxd refuses with 409 `task_in_progress` while a task is running
        (`v1StopSandbox` asks runtimed before stopping), which is exactly the
        guard we want: a caller that mistimes this cannot kill a working agent.
        Best-effort — a pause that fails to sleep is a pause that costs memory,
        not a broken Run.
        """
        try:
            r = await self._req("POST", f"/v1/sandboxes/{sandbox_id}/stop")
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    async def validate_manifest(self, raw: str) -> list[str]:
        """Errors sandboxd finds in a `sandbox.yaml`, empty when it accepts it.

        The control plane owns the schema (`internal/manifest`), so asking it
        beats re-implementing the rules here — v1 is a closed key set, and a
        manifest it rejects means no web process and therefore no preview at
        all. A validator that is unreachable returns no errors: it is a
        pre-flight check, not a gate.
        """
        try:
            r = await self._req("POST", "/v1/runtime/manifest/validate",
                                json={"manifest": raw})
            r.raise_for_status()
            return [str(e) for e in (r.json().get("errors") or [])]
        except httpx.HTTPError:
            return []

    async def keepalive(self, sandbox_id: str, minutes: int) -> None:
        """Hold off sandboxd's idle reaper for the next `minutes`.

        The reaper stops (docker stop) any running sandbox whose last_active_at
        is older than the instance-wide threshold — 35 minutes here. Only the
        exec endpoints and sandbox creation bump that timestamp: the async task
        API does not, so an agent working for longer than the threshold is
        killed mid-task with nothing in flight to protect it.

        Best-effort by design. The route lives on sandboxd's internal surface
        (no /v1 prefix), so a version without it answers 404 — and losing a
        keepalive must never fail a run that is otherwise healthy.
        """
        try:
            await self._req("POST", f"/sandbox/{sandbox_id}/keepalive",
                            json={"until": int(time.time()) + minutes * 60})
        except httpx.HTTPError:
            pass

    async def exec_cmd(self, sandbox_id: str, cmd: list[str],
                       timeout_s: float = 60.0) -> dict:
        """Run a command in the sandbox without spawning the agent.

        For anything mechanical — start a server, probe a port, read a log —
        this is what `submit_task` should not be used for: an agent task costs a
        model call and the better part of a minute, this costs a round trip.

        Two things sandboxd does NOT do for us (`internal/docker/docker.go`,
        `Client.Exec`): it passes neither `-u` nor `-w`, so the command runs as
        the image's user in the image's WORKDIR — `cd` into the app directory
        yourself. Like keepalive, the route lives on the internal surface with
        no `/v1` prefix; unlike keepalive it also bumps `last_active_at`, so a
        polling loop built on it needs no keepalive of its own.

        Returns sandboxd's `{"stdout", "stderr", "exit_code"}`. A non-zero exit
        code is a normal answer, not an error — only transport and HTTP failures
        raise.
        """
        r = await self._req("POST", f"/sandbox/{sandbox_id}/exec",
                            json={"cmd": cmd}, timeout=timeout_s)
        r.raise_for_status()
        return r.json()

    async def submit_task(self, sandbox_id: str, prompt: str, timeout_s: int,
                          continue_session: bool | None = None,
                          model: str | None = None) -> str:
        """Submit an agent task. `continue_session` mirrors sandboxd's tri-state
        `continue` field (control-plane `v1_sandbox_tasks.go`):

        - `None` — the field is omitted and the platform decides. Its default is
          **continue whenever the sandbox already has a session**, so an omitted
          field is not "fresh"; every stage after the first inherits the previous
          stage's whole context.
        - `True` — force `claude --continue` (resume an interrupted stage).
        - `False` — force a brand-new session.

        Passing it explicitly is a cost decision, not cosmetics: an inherited
        context is re-sent on every call of the new stage, and a stage that also
        switches model (executor → reviewer) invalidates the prompt cache, so the
        entire inherited context is re-billed at write price on the first call.
        """
        body: dict = {"prompt": prompt, "agent": "claude-code", "timeout_s": timeout_s}
        if continue_session is not None:
            body["continue"] = continue_session
        if model:
            body["model"] = model
        r = await self._req("POST", f"/v1/sandboxes/{sandbox_id}/tasks", json=body)
        r.raise_for_status()
        return r.json()["id"]

    async def list_tasks(self, sandbox_id: str) -> list[dict]:
        r = await self._req("GET", f"/v1/sandboxes/{sandbox_id}/tasks")
        r.raise_for_status()
        data = r.json()
        return data["tasks"] if isinstance(data, dict) else data

    async def get_task(self, sandbox_id: str, task_id: str) -> dict:
        r = await self._req("GET", f"/v1/sandboxes/{sandbox_id}/tasks/{task_id}")
        r.raise_for_status()
        return r.json()

    async def cancel_task(self, sandbox_id: str, task_id: str) -> None:
        try:
            await self._req("POST", f"/v1/sandboxes/{sandbox_id}/tasks/{task_id}/cancel")
        except httpx.HTTPError:
            pass

    async def git_commit(self, app_id: str, message: str) -> dict:
        r = await self._req("POST", f"/v1/apps/{app_id}/git/commit", json={"message": message})
        r.raise_for_status()
        return r.json()

    # Repo-local config keys sandboxd's pre-push audit refuses to push with
    # (control-plane/internal/gitimport/push.go, dangerousConfigKey). All of
    # them make a host-side git command run something of the repo's choosing.
    UNSAFE_GIT_KEYS = ("core.hooksPath", "core.sshCommand", "core.fsmonitor",
                       "http.proxy")

    async def sanitize_git_config(self, sandbox_id: str) -> list[str]:
        """Drop the repo-local git config keys that would block a push.

        `pnpm install` in any husky-using repo sets `core.hooksPath=.husky/_`,
        and sandboxd then rejects the push with `unsafe_repo_config` — run #45
        lost a finished, advisor-approved plan to exactly that. Removing the
        keys is not a way around the audit: the audit exists so a host-side
        git command cannot be made to execute the repo's hooks, and unsetting
        them is what actually makes that true. They are local to `.git/config`,
        so nothing the user's repository contains is touched. `url.*.insteadOf`
        is deliberately not swept — it is never a build artifact, and a repo
        that rewrites push URLs deserves the refusal.

        Best-effort, returns the keys it removed.
        """
        removed: list[str] = []
        for key in self.UNSAFE_GIT_KEYS:
            try:
                r = await self._req("POST", f"/sandbox/{sandbox_id}/exec", json={
                    "cmd": ["git", "-C", "/home/sandbox/workspace/app",
                            "config", "--local", "--unset-all", key]})
                # git exits 5 when the key is not set — the normal case.
                if r.status_code < 400 and r.json().get("exit_code") == 0:
                    removed.append(key)
            except (httpx.HTTPError, ValueError):
                continue
        return removed

    async def git_push(self, app_id: str, branch: str) -> dict:
        r = await self._req("POST", f"/v1/apps/{app_id}/git/push", json={"branch": branch})
        r.raise_for_status()
        return r.json()

    async def list_files(self, sandbox_id: str, path: str = "",
                         recursive: bool = False) -> list[dict]:
        params = {"path": path}
        if recursive:
            params["recursive"] = "true"
        r = await self._req("GET", f"/v1/sandboxes/{sandbox_id}/files", params=params)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json().get("entries") or []

    async def read_file(self, sandbox_id: str, path: str) -> bytes | None:
        # files/content caps single reads at 2 MiB (sandboxd) — larger files
        # must go through export_zip.
        r = await self._req("GET", f"/v1/sandboxes/{sandbox_id}/files/content",
                            params={"path": path})
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.content

    async def export_zip(self, sandbox_id: str) -> bytes:
        r = await self._req("GET", f"/v1/sandboxes/{sandbox_id}/export")
        r.raise_for_status()
        return r.content
