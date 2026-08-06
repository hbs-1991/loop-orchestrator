import asyncio
import hashlib
import hmac
import json
import re

from fastapi import APIRouter, FastAPI, Request, Response

from . import db as dbmod
from . import issue_tasks as it

router = APIRouter()

_ISSUE_BRANCH_RE = re.compile(r"loop/issue-(\d+)")

_ISSUE_ACTIONS = {"labeled", "unlabeled", "closed", "reopened"}


def _spawn_tick(app: FastAPI, repo: str, seed_issues: list[dict]) -> None:
    """Kick a scheduler tick in the background; the webhook must answer fast."""
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is None:
        return
    tasks = getattr(app.state, "tick_tasks", None)
    if tasks is None:
        tasks = app.state.tick_tasks = set()
    task = asyncio.create_task(scheduler.tick(repo, seed_issues=seed_issues))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _ready_seeds(payload: dict) -> list[dict]:
    """The payload's own issue, when it is open and labeled loop:ready.

    GitHub's ?labels= listing lags label writes by seconds, so the tick this
    delivery triggers may not see the issue it is about — seeding it from the
    payload closes that gap.
    """
    issue = payload.get("issue") or {}
    labels = [(label.get("name") if isinstance(label, dict) else str(label))
              for label in issue.get("labels") or []]
    if issue.get("state") == "open" and "loop:ready" in labels:
        return [issue]
    return []


async def _link_issue_task(db, run, repo: str, issue_number: int) -> None:
    """Attach a PR-mode run to its backlog chain: lane, issue and TG topic."""
    task = await it.get_task(db, repo, issue_number)
    if task is None:
        return
    run.issue_number = issue_number
    run.lane = task.lane
    planning = await dbmod.get_run(db, task.run_id) if task.run_id else None
    if planning is not None and planning.tg_thread_id is not None:
        run.tg_thread_id = planning.tg_thread_id
    await dbmod.save_run(db, run)
    await it.set_run(db, repo, issue_number, run.id)
    await it.set_topic(db, repo, issue_number, run.tg_thread_id)


def _dedup_lock(app: FastAPI) -> asyncio.Lock:
    """Lock guarding the check-then-insert of the "one active Run per PR" invariant.

    Created lazily and stored on app.state: building it takes no await, so
    concurrent handlers on the same loop always get the very same lock.
    """
    lock = getattr(app.state, "dedup_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.dedup_lock = lock
    return lock


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> Response:
    settings = request.app.state.settings
    body = await request.body()
    if not verify_signature(settings.github_webhook_secret, body,
                            request.headers.get("X-Hub-Signature-256")):
        return Response(status_code=401)
    event = request.headers.get("X-GitHub-Event")
    if event in ("issues", "issue_comment"):
        payload = json.loads(body)
        wanted = _ISSUE_ACTIONS if event == "issues" else {"created"}
        if payload.get("action") in wanted:
            _spawn_tick(request.app, payload["repository"]["full_name"],
                        _ready_seeds(payload))
        return Response(status_code=204)
    if event != "pull_request":
        return Response(status_code=204)
    payload = json.loads(body)
    if payload.get("action") != "labeled" or payload.get("label", {}).get("name") != "loop:run":
        return Response(status_code=204)
    pr = payload["pull_request"]
    if pr.get("state") != "open":
        return Response(status_code=204)

    repo = payload["repository"]["full_name"]
    number = pr["number"]
    db = request.app.state.db
    # Duplicate deliveries of the same event may run concurrently: check and insert
    # atomically, otherwise two Runs would work on the same PR branch at once.
    async with _dedup_lock(request.app):
        existing = await dbmod.active_run_for_pr(db, repo, number)
        run = None if existing is not None else await dbmod.create_run(
            db, repo=repo, pr_number=number, head_branch=pr["head"]["ref"],
            pr_title=pr.get("title"))
    if run is None:
        await request.app.state.tg.send(
            f"⚠️ {repo}#{number}: Run #{existing.id} is already active ({existing.state}) — "
            f"the new run was rejected. Wait for it to finish and re-apply the label.")
        return Response(status_code=202)

    m = _ISSUE_BRANCH_RE.fullmatch(pr["head"]["ref"])
    if m:
        await _link_issue_task(db, run, repo, int(m.group(1)))
    request.app.state.worker.enqueue(run.id)
    return Response(status_code=202)
