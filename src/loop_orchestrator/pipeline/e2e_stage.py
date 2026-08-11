"""The e2e stage: Playwright scenarios written from the spec, and their videos.

Shaped like `review_stage`: a verdict, capped fix rounds, an escalation that
records itself on the Run instead of failing it. The video delivery lives here
too — it is the one part that reads files out of the sandbox, and it runs
either with the approval request or with the final report, never both.
"""

import json
from pathlib import PurePosixPath

from .. import db as dbmod
from ..clients.tg_card import run_title
from ..e2e import (
    E2E_DIR,
    MAX_VIDEO_BYTES,
    E2EVerdict,
    E2EVerdictError,
    build_e2e_fix_prompt,
    build_e2e_prompt,
    e2e_report_dict,
    extract_from_zip,
    parse_e2e_verdict,
    select_video_paths,
)
from ..models import E2E_TESTING, Run
from ..secrets import load_repo_secrets
from . import clock
from .constants import MAX_TASK_TIMEOUT_S
from .errors import ReviewDeadline, ReviewTaskError


class E2EMixin:
    async def _finish_e2e(self, run: Run, status: str, summary: str,
                          verdict: E2EVerdict | None) -> None:
        run.e2e_status = status
        run.e2e_json = json.dumps(e2e_report_dict(summary, verdict), ensure_ascii=False)
        await dbmod.save_run(self.db, run)
        await dbmod.add_event(self.db, run.id, E2E_TESTING, E2E_TESTING,
                              f"e2e finished: {status}")

    async def _e2e(self, run: Run) -> None:
        # Like _review: a fresh run.timeout_minutes budget covers the whole
        # e2e+fix cycle; rate-limit pauses extend the deadline.
        deadline = clock.monotonic() + run.timeout_minutes * 60
        task_timeout_s = min(run.timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        env = json.loads(run.e2e_env_json) if run.e2e_env_json else {}
        # Re-read rather than persist: secrets belong on the sandbox's disk and
        # in the server-side file, never in the run record. Only their names
        # reach a prompt (secrets.source_hint).
        secrets = load_repo_secrets(self.settings.secrets_dir, run.repo)
        prompt = build_e2e_prompt(run.spec_path, run.run_cmd, env, secrets)
        retried = False
        while True:
            try:
                # Fresh: the prompt already carries the spec, the run command,
                # the e2e environment and the secrets hint, so nothing of the
                # executor's session is needed — only its files on disk.
                task, deadline = await self._run_sandbox_task(
                    run, prompt, task_timeout_s, deadline,
                    model=self.settings.e2e_model or None,
                    continue_session=False, trace_stage="e2e")
                verdict = parse_e2e_verdict(task.get("agent_message_final")
                                            or task.get("agent_message") or "")
            except ReviewDeadline:
                await self._finish_e2e(run, "escalated",
                                       "e2e interrupted by run timeout", None)
                return
            except (ReviewTaskError, E2EVerdictError) as e:
                if retried:
                    await self._finish_e2e(run, "skipped", f"e2e skipped: {e}", None)
                    return
                retried = True
                await dbmod.add_event(self.db, run.id, E2E_TESTING, E2E_TESTING,
                                      f"e2e attempt failed, retrying once: {e}")
                continue
            retried = False
            if verdict.verdict == "passed":
                await self._finish_e2e(run, "passed", verdict.summary, verdict)
                return
            if run.e2e_iteration >= run.e2e_max_iterations:
                await self._finish_e2e(run, "escalated", verdict.summary, verdict)
                return
            run.e2e_iteration += 1
            await dbmod.save_run(self.db, run)
            failing = sum(1 for t in verdict.tests if t.status == "failed")
            await dbmod.add_event(self.db, run.id, E2E_TESTING, E2E_TESTING,
                                  f"e2e fix iteration {run.e2e_iteration}: "
                                  f"{failing} failing test(s)")
            try:
                # Fresh: the failing scenarios, the spec and the harness (run
                # command, e2e env, secret names) are all in the prompt, so the
                # fixer can reproduce a failure without the tester's session.
                _, deadline = await self._run_sandbox_task(
                    run, build_e2e_fix_prompt(verdict, run.test_cmd, run.spec_path,
                                              run.run_cmd, env, secrets),
                    task_timeout_s, deadline, continue_session=False,
                    trace_stage="e2e-fix")
            except ReviewDeadline:
                await self._finish_e2e(run, "escalated",
                                       "e2e interrupted by run timeout", verdict)
                return
            except ReviewTaskError as e:
                await self._finish_e2e(run, "escalated",
                                       f"fix task failed: {e}", verdict)
                return

    async def _send_e2e_videos(self, run: Run) -> None:
        if run.e2e_status not in ("passed", "escalated"):
            return
        report = json.loads(run.e2e_json or "{}")
        paths = select_video_paths(run.e2e_status, report)
        if not paths:
            return
        try:
            entries = await self.sb.list_files(run.sandbox_id, E2E_DIR)
            sizes = {e["path"]: e.get("size", 0) for e in entries
                     if e.get("type") == "file"}
            wanted = [p for p in paths
                      if p in sizes and sizes[p] <= MAX_VIDEO_BYTES]
            videos: dict[str, bytes] = {}
            small = [p for p in wanted if sizes[p] <= 2 * 1024 * 1024]
            large = [p for p in wanted if sizes[p] > 2 * 1024 * 1024]
            for p in small:
                data = await self.sb.read_file(run.sandbox_id, p)
                if data:
                    videos[p] = data
            if large:
                videos.update(extract_from_zip(
                    await self.sb.export_zip(run.sandbox_id), large))
            for p in wanted:
                if p in videos:
                    name = PurePosixPath(p).name
                    await self.tg.send_video(
                        videos[p], name, f"🎬 {run_title(run)} — {name}",
                        thread_id=run.tg_thread_id)
            skipped = len(paths) - len(videos)
            if skipped:
                await self.tg.send(
                    f"⚠️ {run_title(run)}: {skipped} e2e video(s) skipped "
                    "(missing or over 45 MB).", thread_id=run.tg_thread_id)
        except Exception:  # noqa: BLE001 — video delivery must never fail the run
            await self.tg.send(f"⚠️ {run_title(run)}: e2e video could not be delivered.",
                               thread_id=run.tg_thread_id)
