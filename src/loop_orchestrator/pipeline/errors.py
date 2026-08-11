"""The exceptions the pipeline stages raise at each other.

They live apart from the stages so that a module raising one and a module
catching it need not import each other — `RunFailure` in particular is raised
from every stage and caught in exactly two places (`process`,
`process_planning`).
"""


class RunFailure(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


class ExecutionTimeout(Exception):
    """Agent task used up run.timeout_minutes of actual working time."""


class ReviewTaskError(Exception):
    """The review or fix task failed for a non-rate-limit reason."""


class ReviewDeadline(Exception):
    """The run's review time budget ran out."""


class SyncError(Exception):
    """Conflict resolution could not deliver a merged PR branch."""
