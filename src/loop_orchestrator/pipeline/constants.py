"""Budgets and the failure vocabulary shared by every agent-running stage.

`_execute` and `_run_sandbox_task_inner` are two separate polling loops on
purpose (they raise different exceptions and resume with different prompts),
but they classify a failed task the same way — that classification lives here
so the two cannot drift apart.
"""

RATE_LIMIT_MARKERS = ("rate limit", "usage limit", "limit reached")

# Claude Code surfaces API-level failures ("API Error: Response stalled
# mid-stream", dropped connections) by failing the whole task, but the agent's
# session in the sandbox survives — resubmitting with continue_session picks
# the work up where it stopped instead of losing the stage. Checked after
# RATE_LIMIT_MARKERS, which need the long pause, not an instant resume.
TRANSIENT_AGENT_MARKERS = ("api error", "connection error", "econnreset",
                           "socket hang up", "fetch failed")

MAX_TASK_TIMEOUT_S = 86400

CONTINUE_PROMPT = "Continue the previous task from where it stopped."


def failure_blob(task: dict) -> str:
    """Everything a finished task said about itself, lowercased for matching."""
    return " ".join(filter(None, (
        task.get("error_message"), task.get("failure_reason"),
        task.get("agent_message_final"), task.get("agent_message"),
    ))).lower()


def is_rate_limited(blob: str) -> bool:
    return any(m in blob for m in RATE_LIMIT_MARKERS)


def is_transient(blob: str) -> bool:
    return any(m in blob for m in TRANSIENT_AGENT_MARKERS)
