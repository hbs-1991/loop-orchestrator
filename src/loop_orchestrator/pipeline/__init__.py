"""The Run pipeline: one stage per module, composed into `Pipeline` by `core`.

Reading order, roughly the order a Run goes through them:

- `core` — the `Pipeline` class and `process`, the PR Run's state machine
- `prepare` — `.loop.yml`, the app, the sandbox, the secrets and the context
- `execute` — the executor agent working the plan against the run's budget
- `review_stage` / `e2e_stage` / `contract_stage` — the verdict-producing stages
- `publish` — the two-phase publication (temp branch, then fast-forward)
- `preview` — the approval pause: the preview server and the sleeping sandbox
- `planning_stage` — the planning Run's own, shorter state machine
- `gitsync` — resolving a PR branch that conflicts with its base
- `reporting` — labels, PR comments, Telegram, and the `fail` funnel

Underneath them: `sandbox_tasks` (submit and poll one agent task, surviving a
busy, asleep or unreachable platform), `tracing_mixin`, and the leaves —
`errors`, `constants`, `clock`.

This module re-exports the surface the rest of the orchestrator and the tests
import, so the split stayed invisible to every caller of the old
`pipeline.py`.
"""

from .constants import (
    CONTINUE_PROMPT,
    MAX_TASK_TIMEOUT_S,
    RATE_LIMIT_MARKERS,
    TRANSIENT_AGENT_MARKERS,
)
from .core import Pipeline
from .errors import (
    ExecutionTimeout,
    ReviewDeadline,
    ReviewTaskError,
    RunFailure,
    SyncError,
)
from .gitsync import SYNC_TASK_TIMEOUT_S, build_sync_prompt, sync_app_name
from .prepare import app_name, build_prompt, planning_app_name
from .preview import (
    PREVIEW_APP_DIR,
    PREVIEW_DEFAULT_PORT,
    PREVIEW_LOG,
    PREVIEW_MANIFEST,
    PREVIEW_POLL_SECONDS,
    PREVIEW_READY_TIMEOUT_S,
    build_preview_manifest,
    build_preview_script,
    manifest_guard_script,
    port_probe_argv,
)

__all__ = [
    "CONTINUE_PROMPT",
    "MAX_TASK_TIMEOUT_S",
    "PREVIEW_APP_DIR",
    "PREVIEW_DEFAULT_PORT",
    "PREVIEW_LOG",
    "PREVIEW_MANIFEST",
    "PREVIEW_POLL_SECONDS",
    "PREVIEW_READY_TIMEOUT_S",
    "RATE_LIMIT_MARKERS",
    "SYNC_TASK_TIMEOUT_S",
    "TRANSIENT_AGENT_MARKERS",
    "ExecutionTimeout",
    "Pipeline",
    "ReviewDeadline",
    "ReviewTaskError",
    "RunFailure",
    "SyncError",
    "app_name",
    "build_preview_manifest",
    "build_preview_script",
    "build_prompt",
    "build_sync_prompt",
    "manifest_guard_script",
    "planning_app_name",
    "port_probe_argv",
    "sync_app_name",
]
