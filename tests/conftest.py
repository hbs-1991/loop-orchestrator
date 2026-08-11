"""Shared client fakes for the pipeline/worker tests."""
import httpx
import pytest

from loop_orchestrator import db as dbmod


class FakeGitHub:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.pr_files: list[str] = []
        self.branch_shas: dict[str, str] = {}
        self.labels_added: list[list[str]] = []
        self.labels_removed: list[str] = []
        self.comments: list[str] = []
        self.ff_calls: list[tuple[str, str]] = []
        self.deleted_branches: list[str] = []
        self.ff_error: Exception | None = None
        self.merges: list[tuple[int, str | None]] = []
        self.merge_error: Exception | None = None
        self.label_error: Exception | None = None
        self.pr_info: dict = {"mergeable": True, "mergeable_state": "clean",
                              "base": {"ref": "main"}}
        self.branch_updates: list[int] = []
        self.behind = 0                             # commits base has, head lacks
        self.compares: list[tuple[str, str]] = []
        self.check_runs: list[dict] = []
        self.required_check_names: list[str] = []  # ruleset on the base branch
        self.default_branch = "main"
        self.ready_issues: list[dict] = []          # list_ready_issues response
        self.issues: dict[int, dict] = {}           # get_issue responses
        self.blocked: dict[int, list[int]] = {}     # issue -> open blocker numbers
        self.deps: dict[int, list[dict]] = {}       # issue -> dependency records
        self.blocking: dict[int, list[dict]] = {}   # issue -> issues it blocks
        self.comments_updated: list[tuple[int, str]] = []
        self.issue_comments: dict[int, list[dict]] = {}
        self.branches_created: list[tuple[str, str]] = []
        self.files_put: list[tuple[str, str, str]] = []  # (branch, path, content)
        self.prs_created: list[dict] = []

    async def get_file(self, repo, ref, path):
        return self.files.get(path)

    async def list_pr_files(self, repo, pr_number):
        return self.pr_files

    async def ensure_labels(self, repo):
        pass

    async def add_labels(self, repo, pr_number, labels):
        if self.label_error is not None:
            raise self.label_error
        self.labels_added.append(labels)

    async def remove_label(self, repo, pr_number, label):
        self.labels_removed.append(label)

    async def create_comment(self, repo, pr_number, body):
        self.comments.append(body)

    async def branch_sha(self, repo, branch):
        return self.branch_shas[branch]

    async def fast_forward(self, repo, branch, sha):
        if self.ff_error:
            raise self.ff_error
        self.ff_calls.append((branch, sha))

    async def delete_branch(self, repo, branch):
        self.deleted_branches.append(branch)

    async def merge_pr(self, repo, pr_number, commit_title=None):
        if self.merge_error:
            raise self.merge_error
        self.merges.append((pr_number, commit_title))

    async def get_pr(self, repo, pr_number):
        return self.pr_info

    async def update_pr_branch(self, repo, pr_number):
        self.branch_updates.append(pr_number)

    async def behind_by(self, repo, base, head):
        self.compares.append((base, head))
        return self.behind

    async def list_check_runs(self, repo, sha):
        return self.check_runs

    async def required_checks(self, repo, branch):
        return self.required_check_names

    async def get_repo_default_branch(self, repo):
        return self.default_branch

    async def get_branch_sha(self, repo, branch):
        return self.branch_shas.get(branch)

    async def create_branch(self, repo, branch, sha):
        self.branches_created.append((branch, sha))
        self.branch_shas[branch] = sha

    async def put_file(self, repo, branch, path, content, message):
        self.files_put.append((branch, path, content))

    async def create_pr(self, repo, head, base, title, body):
        self.prs_created.append({"head": head, "base": base,
                                 "title": title, "body": body})
        return 500 + len(self.prs_created)

    async def list_ready_issues(self, repo, label="loop:ready"):
        return self.ready_issues

    async def issue_blocked_by(self, repo, number):
        if number in self.deps:
            return [d["number"] for d in self.deps[number] if d["state"] == "open"]
        return self.blocked.get(number, [])

    async def issue_dependencies(self, repo, number):
        if number in self.deps:
            return self.deps[number]
        return [{"repo": repo, "number": n, "state": "open"}
                for n in self.blocked.get(number, [])]

    async def issue_blocking(self, repo, number):
        return self.blocking.get(number, [])

    async def find_comment(self, repo, number, marker):
        for c in self.issue_comments.get(number, []):
            if marker in (c.get("body") or ""):
                return c
        return None

    async def update_comment(self, repo, comment_id, body):
        self.comments_updated.append((comment_id, body))

    async def upsert_marked_comment(self, repo, number, marker, body):
        existing = await self.find_comment(repo, number, marker)
        if existing is None:
            await self.create_comment(repo, number, body)
        else:
            await self.update_comment(repo, int(existing["id"]), body)

    async def list_issue_comments(self, repo, number, since=None):
        return self.issue_comments.get(number, [])

    async def get_issue(self, repo, number):
        return self.issues.get(number, {"number": number, "state": "open"})


class FakeSandboxd:
    def __init__(self):
        self.apps_created: list[dict] = []
        self.apps_deleted: list[str] = []
        self.secrets: list[tuple[str, str, str]] = []
        self.tasks_submitted: list[dict] = []
        self.task_results: list[dict] = []  # queue of get_task responses
        self.submit_conflicts = 0           # first N submits fail with 409
        self.tasks_listed: list[dict] = []  # list_tasks response
        self.commit_resp = {"committed": True, "sha": "c1"}
        self.push_resp = {"pushed": True, "branch": "x", "commits": 1}
        self.cancelled: list[str] = []
        self.files: list[dict] = []            # list_files entries
        self.file_contents: dict[str, bytes] = {}
        self.export_bytes: bytes = b""
        self.sandbox_info = {"preview": {"url": "https://s-x-3000.preview.test",
                                         "port": 3000}}
        self.keepalives: list[tuple[str, int]] = []
        self.execs: list[dict] = []            # exec_cmd calls, in order
        self.exec_results: list[dict] = []     # queue, consumed first
        self.exec_default = {"stdout": "", "stderr": "", "exit_code": 0}
        self.started: list[str] = []           # sandboxes woken from 'stopped'
        self.stopped: list[str] = []           # sandboxes slept for a pause
        self.stop_ok = True                    # False = sandboxd refuses the stop
        self.manifests_validated: list[str] = []
        self.manifest_errors: list[str] = []   # what validate_manifest reports
        self.sanitized: list[str] = []         # pre-push git config cleanups
        self.unsafe_git_keys: list[str] = []   # what the cleanup found
        self.files_written: list[tuple[str, str, str]] = []  # (sandbox, path, content)
        self.put_file_error: Exception | None = None

    async def create_app(self, name, repo_url, branch, credential_id, preset=None):
        self.apps_created.append({"name": name, "repo_url": repo_url,
                                  "branch": branch, "preset": preset})
        return f"app-{len(self.apps_created)}"

    async def delete_app(self, app_id):
        if app_id:
            self.apps_deleted.append(app_id)

    async def set_app_secret(self, app_id, key, value):
        self.secrets.append((app_id, key, value))

    async def create_sandbox(self, app_id):
        return f"sb-{app_id}"

    async def get_sandbox(self, sandbox_id):
        return self.sandbox_info

    async def keepalive(self, sandbox_id, minutes):
        self.keepalives.append((sandbox_id, minutes))

    async def exec_cmd(self, sandbox_id, cmd, timeout_s=60.0):
        self.execs.append({"sandbox_id": sandbox_id, "cmd": cmd})
        if self.exec_results:
            return self.exec_results.pop(0)
        return dict(self.exec_default)

    async def start_sandbox(self, sandbox_id):
        self.started.append(sandbox_id)
        self.sandbox_info = {**self.sandbox_info, "status": "running"}
        return True

    async def stop_sandbox(self, sandbox_id):
        if not self.stop_ok:
            return False
        self.stopped.append(sandbox_id)
        self.sandbox_info = {**self.sandbox_info, "status": "stopped"}
        return True

    async def validate_manifest(self, raw):
        self.manifests_validated.append(raw)
        return list(self.manifest_errors)

    async def put_file(self, sandbox_id, path, content):
        if self.put_file_error is not None:
            raise self.put_file_error
        self.files_written.append((sandbox_id, path, content))

    async def submit_task(self, sandbox_id, prompt, timeout_s, continue_session=None,
                          model=None):
        if self.submit_conflicts > 0:
            self.submit_conflicts -= 1
            req = httpx.Request("POST", f"http://sb/v1/sandboxes/{sandbox_id}/tasks")
            raise httpx.HTTPStatusError(
                "409 Conflict", request=req,
                response=httpx.Response(409, request=req))
        self.tasks_submitted.append({"sandbox_id": sandbox_id, "prompt": prompt,
                                     "timeout_s": timeout_s, "continue": continue_session,
                                     "model": model})
        return f"task-{len(self.tasks_submitted)}"

    async def list_tasks(self, sandbox_id):
        return self.tasks_listed

    async def get_task(self, sandbox_id, task_id):
        result = self.task_results.pop(0)
        if isinstance(result, Exception):  # queue transport failures like statuses
            raise result
        return result

    async def cancel_task(self, sandbox_id, task_id):
        self.cancelled.append(task_id)

    async def git_commit(self, app_id, message):
        return self.commit_resp

    async def git_push(self, app_id, branch):
        return self.push_resp

    async def sanitize_git_config(self, sandbox_id):
        self.sanitized.append(sandbox_id)
        return self.unsafe_git_keys

    async def list_files(self, sandbox_id, path="", recursive=False):
        return self.files

    async def read_file(self, sandbox_id, path):
        return self.file_contents.get(path)

    async def export_zip(self, sandbox_id):
        return self.export_bytes


class FakeTG:
    def __init__(self):
        self.sent: list[str] = []
        self.videos: list[tuple[str, str]] = []
        self.video_error: Exception | None = None
        self.card_states: list[str] = []
        self.thread_finished = False
        self.buttons: list[tuple[int, dict]] = []

    async def send(self, text, thread_id=None):
        self.sent.append(text)

    async def start_run_thread(self, run):
        return 777

    async def finish_run_thread(self, run):
        self.thread_finished = True
        self.sent.append(f"thread-finished:{run.id}:{run.state}")

    async def send_card(self, run, events):
        self.card_states.append(run.state)
        return 555

    async def update_card(self, run, events):
        self.card_states.append(run.state)

    async def notify_done(self, run):
        self.sent.append(f"done:{run.id}")
        return 909                              # the merge message's id

    async def set_buttons(self, message_id, markup):
        self.buttons.append((message_id, markup))

    async def notify_failed(self, run):
        self.sent.append(f"failed:{run.id}")

    async def notify_review_escalation(self, run, remaining):
        self.sent.append(f"escalation:{run.id}:{remaining}")

    async def send_video(self, video, filename, caption, thread_id=None):
        if self.video_error:
            raise self.video_error
        self.videos.append((filename, caption))

    async def notify_e2e_escalation(self, run, failed):
        self.sent.append(f"e2e-escalation:{run.id}:{failed}")

    async def notify_awaiting_approval(self, run):
        self.sent.append(f"awaiting:{run.id}")
        return 900

    async def notify_cancelled(self, run, note=""):
        self.sent.append(f"cancelled:{run.id}")

    async def answer_callback(self, callback_id, text):
        self.sent.append(f"cb:{callback_id}:{text}")

    async def clear_buttons(self, message_id):
        self.sent.append(f"clear:{message_id}")

    async def set_webhook(self, url, secret):
        self.sent.append(f"webhook:{url}")


class FakeSettings:
    github_token = "gh-tok"
    github_webhook_secret = "whs"
    secrets_dir = "unset"  # overridden per test via tmp_path
    default_timeout_minutes = 180
    poll_interval_seconds = 0
    rate_limit_retry_minutes = 60
    agent_retry_attempts = 2
    agent_retry_backoff_seconds = 0
    max_concurrent_runs = 4
    git_credential_id = "cred1"
    reviewer_model = "claude-fable-5"
    review_timeout_minutes = 30
    review_max_fix_iterations = 2
    e2e_max_fix_iterations = 2
    e2e_model = ""
    preview_ttl_minutes = 120
    keepalive_minutes = 30
    telegram_webhook_secret = "tgsec"
    promote_label = "promote:staging"
    public_url = ""
    backlog_poll_minutes = 5
    backlog_repos = ""
    planner_model = ""
    advisor_model = "claude-fable-5"
    contract_model = "claude-sonnet-5"
    plan_max_iterations = 3
    # Tracing off by default here too, so every existing test keeps asserting on
    # a pipeline that makes no tracing calls at all.
    otlp_endpoint = ""
    trace_service_name = "loop-orchestrator-test"
    trace_preview_chars = 500
    model_prices = ""

    def admin_ids(self):
        return {1}

    def backlog_repo_list(self):
        return []


@pytest.fixture
async def db(tmp_path):
    conn = await dbmod.connect(str(tmp_path / "loop.db"))
    yield conn
    await conn.close()
