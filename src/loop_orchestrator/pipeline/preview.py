"""The approval pause: a live preview of the branch, and a sandbox asleep behind it.

Everything here is auxiliary — a Run whose preview never comes up still pauses,
still gets approved and still publishes. What the module buys is the link in
the approval message, and the `sandbox.yaml` manifest that lets the paused
sandbox be stopped without taking the link down with it.
"""

import asyncio
import json
import shlex

from .. import db as dbmod
from ..models import AWAITING_APPROVAL, Run
from ..secrets import SECRETS_FILE
from . import clock

# Preview server, started through exec rather than an agent task.
PREVIEW_APP_DIR = "/home/sandbox/workspace/app"  # runtimed's cmd.Dir; exec gets no -w
PREVIEW_LOG = ".loop/preview.log"
# The platform's own runtime manifest, read by runtimed on every container start.
# App-directory-relative, like every path the files API takes. NOT under .loop/:
# sandboxd looks for it next to the app, and it is a legitimate repo file —
# committing it is the app's business, and the executor commits only its own work.
PREVIEW_MANIFEST = "sandbox.yaml"
PREVIEW_DEFAULT_PORT = 3000
PREVIEW_READY_TIMEOUT_S = 180
PREVIEW_POLL_SECONDS = 3


def port_probe_argv(port: int) -> list[str]:
    """argv that exits 0 iff something already listens on `port` in the sandbox.

    python3 rather than curl: it is the one interpreter the sandbox image is
    guaranteed to carry, and `procps` (so, `ps`/`pgrep`) is not installed.
    """
    return ["python3", "-c",
            "import socket,sys;s=socket.socket();s.settimeout(1);"
            f"sys.exit(s.connect_ex(('127.0.0.1',{port})))"]


def build_preview_script(run_cmd: str, port: int,
                         e2e_env: dict[str, str] | None = None) -> str:
    """Shell script that brings the app's web server up in the background.

    Runs through the exec endpoint, so it gets none of an agent's judgement and
    needs none: by the time a Run pauses, the executor has already installed and
    built the app, and the only thing left is to start the command.

    Secrets are sourced from the file the sandbox already carries — the values
    never enter this string, and so never enter a log or a Run record (the same
    reason the stage prompts only ever name the keys).
    """
    exports = "".join(f"export {k}={shlex.quote(v)}\n"
                      for k, v in (e2e_env or {}).items())
    return (
        f"cd {PREVIEW_APP_DIR} || exit 1\n"
        "mkdir -p .loop\n"
        # An exec retried by with_retries after a lost response must not start a
        # second server: the first one holds the port and the second only writes
        # EADDRINUSE into the log.
        f"{shlex.join(port_probe_argv(port))} && exit 0\n"
        "set -a\n"
        f"[ -f {SECRETS_FILE} ] && . {SECRETS_FILE}\n"
        "set +a\n"
        f"{exports}"
        f"nohup {run_cmd} > {PREVIEW_LOG} 2>&1 &\n"
        "echo preview-started\n"
    )


def build_preview_manifest(run_cmd: str, port: int,
                           e2e_env: dict[str, str] | None = None) -> str:
    """`sandbox.yaml` declaring the preview server as the platform's own process.

    The exec-started server above dies with its container, so a paused Run had
    to be kept awake for the whole TTL to keep its link alive. Declared here
    instead, the same command becomes runtimed's `web` process: it is restarted
    whenever the container starts, which is what makes sleeping the pause
    possible — traefik's wake catch-all starts the container on the first hit
    of the preview host and runtimed brings the server back
    ([[decisions/0015-sleep-the-paused-sandbox]] in the wiki).

    Verified against the platform: `runtimed/process.go` runs the command with
    `bash -lc` in the app directory, so the login PATH (pnpm, node) is in scope
    and `.loop/secrets.env` resolves — the same context the exec script gets.
    `web.port` is mandatory whenever `web.command` is set, else the control
    plane rejects the manifest and serves no preview at all.
    """
    exports = "".join(f"export {k}={shlex.quote(v)}; "
                      for k, v in (e2e_env or {}).items())
    command = (f"set -a; [ -f {SECRETS_FILE} ] && . {SECRETS_FILE}; set +a; "
               f"{exports}{run_cmd}")
    # json.dumps gives a correctly escaped YAML double-quoted scalar, which a
    # hand-rolled quote would get wrong the first time a command contains one.
    return ("version: 1\n"
            "web:\n"
            f"  command: {json.dumps(command)}\n"
            f"  port: {port}\n"
            "  health_path: /\n")


def manifest_guard_script() -> str:
    """Exit 0 when it is safe to write our own `sandbox.yaml`, else non-zero.

    Two jobs. It refuses when the repository tracks a `sandbox.yaml` of its own
    — that file belongs to the app, and rewriting it would ride into the next
    revise commit and the PR diff. And when the path is free it adds it to
    `.git/info/exclude`, which is per-clone and never committed, so a `git add
    -A` by the revise agent cannot pick our file up.
    """
    return (
        f"cd {PREVIEW_APP_DIR} || exit 1\n"
        f"git ls-files --error-unmatch {PREVIEW_MANIFEST} >/dev/null 2>&1 && exit 3\n"
        "mkdir -p .git/info\n"
        f"grep -qxF {PREVIEW_MANIFEST} .git/info/exclude 2>/dev/null "
        f"|| echo {PREVIEW_MANIFEST} >> .git/info/exclude\n"
        "echo manifest-armed\n"
    )


class PreviewMixin:
    async def _start_preview(self, run: Run) -> bool:
        """Best-effort: bring the web server up and record the sandbox preview URL.

        Returns True when the preview can survive the sandbox being stopped —
        i.e. the manifest was accepted and written, so the pause may sleep.

        Through exec, not an agent task. Starting a server is mechanical work —
        the executor has already installed and built the app — and an agent task
        buys nothing here while costing a model call and the better part of a
        minute. Worse, it is a *fresh* session by necessity (otherwise it drags
        the previous stage's whole context along), so every call has to restate
        the command, the environment and where the credentials live.

        The URL is recorded only once the port actually answers: a preview link
        that greets a reviewer with a 502 is worse than no link at all.
        """
        if not run.run_cmd:
            return False
        try:
            preview = (await self.sb.get_sandbox(run.sandbox_id)).get("preview") or {}
            port = int(preview.get("port") or PREVIEW_DEFAULT_PORT)
            env = json.loads(run.e2e_env_json) if run.e2e_env_json else {}
            await self.sb.exec_cmd(run.sandbox_id, [
                "sh", "-c", build_preview_script(run.run_cmd, port, env)])
            if not await self._preview_responds(run, port):
                await self._note_preview_failure(run)
                return False
            run.preview_url = preview.get("url") or None
            await dbmod.save_run(self.db, run)
            return await self._arm_preview_manifest(run, port, env)
        except Exception:  # noqa: BLE001 — preview is auxiliary
            return False

    async def _arm_preview_manifest(self, run: Run, port: int,
                                    env: dict[str, str]) -> bool:
        """Declare the running server in `sandbox.yaml` so a wake can restart it.

        The exec-started process answered a moment ago, which is what makes the
        link publishable; this only teaches the platform to bring the same
        command back after a stop. Written *after* the port answered, never
        before: a manifest naming a command that does not work would replace a
        working template preview with none at all.
        """
        raw = build_preview_manifest(run.run_cmd, port, env)
        errors = await self.sb.validate_manifest(raw)
        if errors:
            await dbmod.add_event(
                self.db, run.id, run.state, run.state,
                "preview manifest rejected, the pause will stay awake: "
                + "; ".join(errors)[:400])
            return False
        try:
            res = await self.sb.exec_cmd(run.sandbox_id,
                                         ["sh", "-c", manifest_guard_script()])
            if (res or {}).get("exit_code") != 0:
                # The repository ships its own sandbox.yaml. Overwriting a
                # tracked file would put our preview command in the revise
                # commit and in the PR diff, so the pause stays awake instead —
                # the memory is worth less than a clean diff.
                await dbmod.add_event(
                    self.db, run.id, run.state, run.state,
                    "the repository tracks its own sandbox.yaml; the pause "
                    "stays awake rather than overwriting it")
                return False
            await self.sb.put_file(run.sandbox_id, PREVIEW_MANIFEST, raw)
        except Exception:  # noqa: BLE001 — an awake pause still works
            return False
        return True

    async def _preview_responds(self, run: Run, port: int) -> bool:
        """Poll the sandbox's own port until the server answers.

        No keepalive here: exec bumps `last_active_at` itself, so this loop
        keeps the sandbox awake as a side effect of doing its job.
        """
        deadline = clock.monotonic() + PREVIEW_READY_TIMEOUT_S
        while clock.monotonic() < deadline:
            res = await self.sb.exec_cmd(run.sandbox_id, port_probe_argv(port))
            if res.get("exit_code") == 0:
                return True
            await asyncio.sleep(PREVIEW_POLL_SECONDS)
        return False

    async def _sleep_pause(self, run: Run) -> None:
        """Stop the sandbox for the duration of the pause, and say so.

        The preview link keeps working: traefik's wake catch-all (priority 1,
        so a running sandbox's own router always wins) forwards a hit on the
        preview host to sandboxd, which starts the container and proxies the
        request. Measured on the 2026-08-08 probe: the first hit after a sleep
        takes ~8-14 s and may answer 502 while the server is still binding, the
        next one is a normal 200.
        """
        if not await self.sb.stop_sandbox(run.sandbox_id):
            # Refused (a task slipped in) or unreachable — the keepalive path
            # is gone, but sandboxd's own idle reaper will stop it anyway and
            # the manifest brings the preview back either way.
            await dbmod.add_event(self.db, run.id, run.state, run.state,
                                  "could not sleep the paused sandbox")
            return
        await dbmod.add_event(
            self.db, run.id, run.state, run.state,
            "sandbox stopped for the pause; the preview link wakes it on demand")

    async def _note_preview_failure(self, run: Run) -> None:
        """Keep the reason the preview never came up — the log dies with the app."""
        tail = ""
        try:
            res = await self.sb.exec_cmd(run.sandbox_id, [
                "sh", "-c", f"tail -c 800 {PREVIEW_APP_DIR}/{PREVIEW_LOG} 2>/dev/null"])
            tail = (res.get("stdout") or "").strip()
        except Exception:  # noqa: BLE001 — diagnostics must not break the pause
            pass
        await dbmod.add_event(
            self.db, run.id, run.state, run.state,
            f"preview server never answered on its port; log tail: {tail[-500:] or '(empty)'}")

    async def _notify_awaiting(self, run: Run) -> None:
        try:
            msg_id = await self.tg.notify_awaiting_approval(run)
            if msg_id:
                run.tg_approval_message_id = msg_id
                await dbmod.save_run(self.db, run)
                # Videos ride the pause only when the approval message made
                # it out; otherwise _report_success delivers them at the end
                # (its guard fires on tg_approval_message_id is None) — never
                # both.
                await self._send_e2e_videos(run)
        except Exception:  # noqa: BLE001
            pass

    async def expire_preview(self, run: Run) -> None:
        """TTL sweep: tear down the paused run's sandbox; the run stays paused."""
        try:
            await self.sb.delete_app(run.app_id)
        except Exception:  # noqa: BLE001 — retried on the next sweep
            return
        run.app_id = None
        run.sandbox_id = None
        run.preview_url = None
        run.sandbox_expires_at = None
        await dbmod.save_run(self.db, run)
        await dbmod.add_event(self.db, run.id, AWAITING_APPROVAL, AWAITING_APPROVAL,
                              "preview expired — sandbox deleted")
        await self._refresh_card(run)
