import asyncio
import hashlib
import hmac
import json

import httpx
from fastapi import FastAPI

from loop_orchestrator import db as dbmod
from loop_orchestrator import issue_tasks as it
from loop_orchestrator.models import ACTIVE_STATES, QUEUED
from loop_orchestrator.webhook import router, verify_signature

SECRET = "whsec"


class FakeWorker:
    def __init__(self):
        self.enqueued: list[int] = []

    def enqueue(self, run_id: int) -> None:
        self.enqueued.append(run_id)


class FakeTG:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class FakeSettings:
    github_webhook_secret = SECRET


async def make_app(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = FakeSettings()
    app.state.db = await dbmod.connect(str(tmp_path / "t.db"))
    app.state.worker = FakeWorker()
    app.state.tg = FakeTG()
    return app


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def labeled_payload(label="loop:run", state="open") -> bytes:
    return json.dumps({
        "action": "labeled",
        "label": {"name": label},
        "pull_request": {"number": 5, "state": state, "head": {"ref": "feat/x"},
                         "title": "feat: x"},
        "repository": {"full_name": "o/r"},
    }).encode()


async def post(app, body: bytes, sig: str | None, event: str = "pull_request"):
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if sig is not None:
        headers["X-Hub-Signature-256"] = sig
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post("/webhooks/github", content=body, headers=headers)


def test_verify_signature():
    body = b"hello"
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, "sha256=deadbeef")
    assert not verify_signature(SECRET, body, None)


async def test_creates_run_and_enqueues(tmp_path):
    app = await make_app(tmp_path)
    body = labeled_payload()
    r = await post(app, body, sign(body))
    assert r.status_code == 202
    run = await dbmod.get_run(app.state.db, 1)
    assert run.repo == "o/r" and run.pr_number == 5 and run.state == QUEUED
    assert run.head_branch == "feat/x"
    assert app.state.worker.enqueued == [1]


async def test_webhook_stores_pr_title(tmp_path):
    app = await make_app(tmp_path)
    body = labeled_payload()
    await post(app, body, sign(body))
    run = await dbmod.get_run(app.state.db, 1)
    assert run.pr_title == "feat: x"


async def test_rejects_bad_signature(tmp_path):
    app = await make_app(tmp_path)
    r = await post(app, labeled_payload(), "sha256=deadbeef")
    assert r.status_code == 401
    assert app.state.worker.enqueued == []


async def test_ignores_other_events_and_labels(tmp_path):
    app = await make_app(tmp_path)
    body = labeled_payload()
    assert (await post(app, body, sign(body), event="push")).status_code == 204
    other = labeled_payload(label="bug")
    assert (await post(app, other, sign(other))).status_code == 204
    closed = labeled_payload(state="closed")
    assert (await post(app, closed, sign(closed))).status_code == 204
    assert app.state.worker.enqueued == []


async def test_duplicate_active_run_rejected(tmp_path):
    app = await make_app(tmp_path)
    body = labeled_payload()
    await post(app, body, sign(body))
    r = await post(app, body, sign(body))
    assert r.status_code == 202
    assert app.state.worker.enqueued == [1]  # not enqueued a second time
    assert len(app.state.tg.sent) == 1 and "already active" in app.state.tg.sent[0]


async def test_concurrent_deliveries_create_single_run(tmp_path):
    app = await make_app(tmp_path)
    body = labeled_payload()
    sig = sign(body)
    responses = await asyncio.gather(*(post(app, body, sig) for _ in range(4)))
    assert [r.status_code for r in responses] == [202] * 4
    active = await dbmod.runs_in_states(app.state.db, ACTIVE_STATES)
    assert len(active) == 1
    assert app.state.worker.enqueued == [1]
    assert len(app.state.tg.sent) == 3


class FakeScheduler:
    def __init__(self):
        self.ticks: list[str] = []
        self.seeds: list[list[dict]] = []

    async def tick(self, repo: str, seed_issues: list[dict] | None = None) -> None:
        self.ticks.append(repo)
        self.seeds.append(seed_issues or [])


async def test_issue_labeled_triggers_scheduler_tick(tmp_path):
    app = await make_app(tmp_path)
    app.state.scheduler = FakeScheduler()
    body = json.dumps({"action": "labeled",
                       "label": {"name": "loop:ready"},
                       "repository": {"full_name": "o/r"},
                       "issue": {"number": 7}}).encode()
    r = await post(app, body, sign(body), event="issues")
    assert r.status_code == 204
    await asyncio.sleep(0)  # let the background tick run
    assert app.state.scheduler.ticks == ["o/r"]


async def test_labeled_ready_issue_seeds_tick_from_payload(tmp_path):
    # GitHub's ?labels= listing lags label writes by seconds; the tick must
    # not depend on it for the very issue this delivery is about.
    app = await make_app(tmp_path)
    app.state.scheduler = FakeScheduler()
    issue = {"number": 7, "title": "T", "state": "open",
             "labels": [{"name": "loop:ready"}, {"name": "loop:lane:auth"}]}
    body = json.dumps({"action": "labeled", "label": {"name": "loop:ready"},
                       "repository": {"full_name": "o/r"},
                       "issue": issue}).encode()
    r = await post(app, body, sign(body), event="issues")
    assert r.status_code == 204
    await asyncio.sleep(0)
    assert app.state.scheduler.seeds == [[issue]]


async def test_closed_issue_is_not_seeded(tmp_path):
    app = await make_app(tmp_path)
    app.state.scheduler = FakeScheduler()
    body = json.dumps({"action": "closed",
                       "repository": {"full_name": "o/r"},
                       "issue": {"number": 7, "title": "T", "state": "closed",
                                 "labels": [{"name": "loop:ready"}]}}).encode()
    r = await post(app, body, sign(body), event="issues")
    assert r.status_code == 204
    await asyncio.sleep(0)
    assert app.state.scheduler.ticks == ["o/r"]
    assert app.state.scheduler.seeds == [[]]


async def test_issue_comment_triggers_tick(tmp_path):
    app = await make_app(tmp_path)
    app.state.scheduler = FakeScheduler()
    body = json.dumps({"action": "created",
                       "repository": {"full_name": "o/r"},
                       "issue": {"number": 7}, "comment": {"body": "hi"}}).encode()
    r = await post(app, body, sign(body), event="issue_comment")
    assert r.status_code == 204
    await asyncio.sleep(0)
    assert app.state.scheduler.ticks == ["o/r"]


async def test_loop_run_label_links_execution_run_to_issue_task(tmp_path):
    app = await make_app(tmp_path)
    db = app.state.db
    planning = await dbmod.create_planning_run(db, "o/r", 7, "loop/issue-7", "T", "auth")
    planning.tg_thread_id = 777
    planning.state = "done"
    await dbmod.save_run(db, planning)
    await it.upsert_task(db, "o/r", 7, "T", "auth")
    await it.set_run(db, "o/r", 7, planning.id)
    await it.set_state(db, "o/r", 7, it.RUNNING)

    body = json.dumps({"action": "labeled", "label": {"name": "loop:run"},
                       "repository": {"full_name": "o/r"},
                       "pull_request": {"number": 51, "state": "open",
                                        "title": "T",
                                        "head": {"ref": "loop/issue-7"}}}).encode()
    r = await post(app, body, sign(body))
    assert r.status_code == 202
    task = await it.get_task(db, "o/r", 7)
    run = await dbmod.get_run(db, task.run_id)
    assert run.kind == "pr" and run.pr_number == 51
    assert (run.issue_number, run.lane, run.tg_thread_id) == (7, "auth", 777)
    assert task.topic_id == 777
