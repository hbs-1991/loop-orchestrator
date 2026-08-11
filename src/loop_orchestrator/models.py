from dataclasses import dataclass

QUEUED = "queued"
PREPARING = "preparing"
EXECUTING = "executing"
REVIEWING = "reviewing"
E2E_TESTING = "e2e_testing"
PUBLISHING = "publishing"
REPORTING = "reporting"
DONE = "done"
FAILED = "failed"
STAGING = "staging"
AWAITING_APPROVAL = "awaiting_approval"
CANCELLED = "cancelled"
PLANNING = "planning"
CONTRACTING = "contracting"

ACTIVE_STATES = {QUEUED, PREPARING, PLANNING, EXECUTING, REVIEWING, E2E_TESTING,
                 CONTRACTING, STAGING, AWAITING_APPROVAL, PUBLISHING, REPORTING}

# States from which a human may cancel a run (before its work is staged).
CANCELABLE = {QUEUED, PREPARING, PLANNING, EXECUTING, REVIEWING, E2E_TESTING,
              CONTRACTING}


@dataclass
class Run:
    id: int
    repo: str
    pr_number: int
    head_branch: str
    state: str
    app_id: str | None = None
    sandbox_id: str | None = None
    task_id: str | None = None
    spec_path: str | None = None
    plan_path: str | None = None
    prompt: str | None = None
    timeout_minutes: int = 180
    error: str | None = None
    summary: str | None = None
    test_cmd: str | None = None
    review_enabled: bool = True
    review_max_iterations: int = 2
    review_iteration: int = 0
    review_status: str | None = None  # clean | escalated | skipped
    review_json: str | None = None
    run_cmd: str | None = None
    e2e_enabled: bool = False
    e2e_max_iterations: int = 2
    e2e_iteration: int = 0
    e2e_status: str | None = None  # passed | escalated | skipped
    e2e_json: str | None = None
    e2e_env_json: str | None = None
    pr_title: str | None = None
    tg_thread_id: int | None = None
    tg_card_message_id: int | None = None
    approval_mode: str = "always"  # always | never — snapshot of .loop.yml
    staging_branch: str | None = None
    preview_url: str | None = None
    sandbox_expires_at: str | None = None  # UTC "YYYY-MM-DD HH:MM:SS"
    merged_at: str | None = None
    tg_approval_message_id: int | None = None
    # The "finished" message that carries the merge buttons. Kept so the reaper
    # can repaint that keyboard as the PR's gate state changes.
    tg_merge_message_id: int | None = None
    kind: str = "pr"  # pr | planning
    issue_number: int | None = None
    lane: str | None = None
    # Planning knobs, read from `.loop.yml` at prepare. A model of None means
    # "use the LOOP_* setting"; plan_max_iterations of None means the same.
    planner_model: str | None = None
    advisor_enabled: bool = True
    advisor_model: str | None = None
    plan_max_iterations: int | None = None
    # Set at prepare: only a Run tied to an issue can hand anything downstream.
    contract_enabled: bool = False
    contract_status: str | None = None  # produced | none | skipped | failed
    contract_json: str | None = None    # the captured Contract, for Telegram
