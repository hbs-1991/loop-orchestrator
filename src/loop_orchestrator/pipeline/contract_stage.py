"""The contracting stage: what this Run built, written down for its consumers.

Two halves, deliberately far apart in time. The capture runs right after the
code is reviewed and stores the contract; the issue comment is published only
once the code is final, from the publishing stage — see
`_publish_contract_comment`.
"""

import json
import logging
from dataclasses import asdict

import httpx

from .. import db as dbmod
from ..contracts import (
    COMMENT_MARKER,
    Contract,
    ContractError,
    build_contract_prompt,
    parse_contract_output,
    render_contract_comment,
)
from ..models import CONTRACTING, Run
from . import clock
from .constants import MAX_TASK_TIMEOUT_S
from .errors import ReviewDeadline, ReviewTaskError, RunFailure

log = logging.getLogger(__name__)


class ContractMixin:
    async def _finish_contract(self, run: Run, status: str, detail: str) -> None:
        run.contract_status = status
        await dbmod.save_run(self.db, run)
        await dbmod.add_event(self.db, run.id, CONTRACTING, CONTRACTING,
                              f"contracting: {status} — {detail}")

    async def _contracting(self, run: Run) -> None:
        """Describe the interface this Run built, for the tasks it blocks.

        Never fatal: the work is already reviewed, and a consumer that gets no
        contract stops at its own planner gate instead of guessing.
        """
        try:
            blocking = await self.gh.issue_blocking(run.repo, run.issue_number)
        except Exception as e:  # noqa: BLE001 — a lookup failure is not a Run failure
            return await self._finish_contract(run, "skipped",
                                               f"blocking lookup failed: {e}")
        if not blocking:
            return await self._finish_contract(run, "skipped",
                                               "the issue blocks nothing")
        timeout_s = min(self.settings.review_timeout_minutes * 60, MAX_TASK_TIMEOUT_S)
        deadline = clock.monotonic() + timeout_s
        try:
            # Fresh session: the prompt names the branch to diff against, and a
            # describing agent that inherits the executor's session inherits its
            # belief about what it built rather than reading what it did build.
            task, _ = await self._run_sandbox_task(
                run, build_contract_prompt(run.head_branch), timeout_s, deadline,
                model=self.settings.contract_model or None,
                continue_session=False, trace_stage="contract")
            contract = parse_contract_output(task.get("agent_message_final")
                                             or task.get("agent_message") or "")
        except ReviewDeadline:
            return await self._finish_contract(run, "failed",
                                               "contracting timed out")
        except (ReviewTaskError, ContractError, RunFailure,
                httpx.HTTPStatusError) as e:
            # RunFailure and a 4xx are in here for the Locked Decision, not for
            # tidiness: a dead sandbox is named in the spec's failure table as a
            # contracting failure that still proceeds. Letting it fail the Run
            # here would blame `contracting` for a sandbox that `staging` is
            # about to reject anyway, under its own name.
            return await self._finish_contract(run, "failed",
                                               f"contract not captured: {e}")
        head_sha = ""
        try:
            head_sha = await self.gh.branch_sha(run.repo, run.head_branch)
        except Exception:  # noqa: BLE001 — provenance only
            pass
        await dbmod.save_contract(
            self.db, run.repo, run.issue_number, run.id, run.pr_number, head_sha,
            contract.contract, contract.sources, contract.breaking_changes)
        # Also on the Run, like review_json/e2e_json: the approval message has to
        # render the contract before the issue comment exists.
        run.contract_json = json.dumps(asdict(contract), ensure_ascii=False)
        await self._finish_contract(
            run, "produced" if contract.outcome == "contract" else "none",
            f"captured for {len(blocking)} dependent issue(s)")

    async def _publish_contract_comment(self, run: Run) -> None:
        """Put the captured contract where a human can read and correct it.

        Published here rather than at capture time: this is the first moment the
        code is final. Best-effort — the stored row is what consumers fall back
        to when the comment never lands.
        """
        if run.issue_number is None or run.contract_status not in ("produced", "none"):
            return
        row = await dbmod.get_contract(self.db, run.repo, run.issue_number)
        if row is None:
            return
        body = render_contract_comment(
            Contract(outcome="contract" if row["contract_md"] else "none",
                     contract=row["contract_md"],
                     sources=json.loads(row["sources_json"]),
                     breaking_changes=json.loads(row["breaking_json"])),
            run.pr_number, row["head_sha"])
        try:
            await self.gh.upsert_marked_comment(run.repo, run.issue_number,
                                                COMMENT_MARKER, body)
        except Exception:  # noqa: BLE001 — the stored contract still reaches consumers
            log.warning("contract comment failed for run=%s", run.id, exc_info=True)
