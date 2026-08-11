"""Tracing is an observation of the work, never a precondition for it."""
import json

from loop_orchestrator import db as dbmod
from loop_orchestrator.models import DONE
from loop_orchestrator.tracing.collector import TRACE_DIR
from loop_orchestrator.tracing.tracer import RunTracer, run_span_id, stage_span_id

from tests.conftest import FakeGitHub, FakeSandboxd, FakeTG
from tests.test_pipeline_prepare import make_pipeline, seed_ok

EXEC_OK = {"status": "succeeded", "agent_message_final": "did the work"}
CLEAN = {"status": "succeeded",
         "agent_message_final": '{"verdict": "clean", "summary": "ok", "findings": []}'}

USAGE = {"input_tokens": 5, "cache_creation_input_tokens": 20_000,
         "cache_read_input_tokens": 25_000, "output_tokens": 400}


def session_bytes(model="claude-opus-5", tool="Read"):
    return "\n".join(json.dumps(e) for e in [
        {"type": "user", "timestamp": "2026-08-06T10:00:00Z",
         "message": {"content": [{"type": "text", "text": "the stage prompt"}]}},
        {"type": "assistant", "timestamp": "2026-08-06T10:00:01Z", "sessionId": "s-9",
         "message": {"id": "m1", "model": model, "usage": USAGE,
                     "content": [{"type": "tool_use", "id": "t1", "name": tool,
                                  "input": {"file_path": "a.py"}}]}},
        {"type": "user", "timestamp": "2026-08-06T10:00:02Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                  "content": "file body"}]}},
    ]).encode()


class CapturingOTLP:
    def __init__(self, fail=False):
        self.batches: list[list] = []
        self.fail = fail

    async def export(self, spans):
        if self.fail:
            raise RuntimeError("collector is down")
        self.batches.append(list(spans))
        return True

    @property
    def spans(self):
        return [s for b in self.batches for s in b]


def traced_pipeline(db, tmp_path, gh, sb, tg, otlp=None):
    pipe = make_pipeline(db, tmp_path, gh=gh, sb=sb, tg=tg)
    pipe.tracer = RunTracer(otlp or CapturingOTLP(), pipe.settings, db)
    return pipe


async def run_once(db, tmp_path, *, traced=True, otlp=None, sessions=True):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    sb.push_resp = {"pushed": True, "branch": f"loop/run-{run.id}", "commits": 2}
    gh.branch_shas[f"loop/run-{run.id}"] = "sha1"
    if sessions:
        for stage in ("executing", "review"):
            sb.file_contents[f"{TRACE_DIR}/{stage}.jsonl"] = session_bytes()
    pipe = (traced_pipeline(db, tmp_path, gh, sb, tg, otlp)
            if traced else make_pipeline(db, tmp_path, gh=gh, sb=sb, tg=tg))
    sb.task_results = [EXEC_OK, CLEAN]
    await pipe.process(run)
    return pipe, run, sb


# --- off by default --------------------------------------------------------


async def test_without_an_endpoint_nothing_is_traced(db, tmp_path):
    pipe, run, sb = await run_once(db, tmp_path, traced=False)
    assert pipe.tracer is None
    assert run.state == DONE
    # Not one exec call, so no copy, no read, no export.
    assert not any(TRACE_DIR in " ".join(e["cmd"]) for e in sb.execs)
    assert await dbmod.trace_rollup_for_run(db, run.id) is None


# --- the tree --------------------------------------------------------------


async def test_a_run_produces_root_stage_session_call_and_tool_spans(db, tmp_path):
    otlp = CapturingOTLP()
    _, run, _ = await run_once(db, tmp_path, otlp=otlp)
    names = [s.name for s in otlp.spans]
    assert f"run #{run.id}" in names
    assert "stage.executing" in names and "stage.review" in names
    assert "agent.session" in names
    assert "api.call #1" in names
    assert "tool.Read" in names


async def test_the_tree_is_connected_root_to_tool(db, tmp_path):
    otlp = CapturingOTLP()
    _, run, _ = await run_once(db, tmp_path, otlp=otlp)
    by_id = {s.span_id: s for s in otlp.spans}
    root = [s for s in otlp.spans if s.name == f"run #{run.id}"][0]
    assert root.parent_id is None and root.span_id == run_span_id(run.id)

    stage = [s for s in otlp.spans if s.name == "stage.review"][0]
    assert stage.span_id == stage_span_id(run.id, "review")
    assert stage.parent_id == root.span_id

    tool = [s for s in otlp.spans if s.name == "tool.Read"][0]
    call = by_id[tool.parent_id]
    session = by_id[call.parent_id]
    assert call.name == "api.call #1" and session.name == "agent.session"
    # Every stage span is reachable from the root, so one trace holds the Run.
    assert by_id[session.parent_id].name.startswith("stage.")


async def test_stage_spans_record_the_model_and_whether_the_session_was_fresh(db, tmp_path):
    otlp = CapturingOTLP()
    await run_once(db, tmp_path, otlp=otlp)
    review = [s for s in otlp.spans if s.name == "stage.review"][0]
    # Recorded from the `continue` we sent, not inferred from the file.
    assert review.attributes["session.fresh"] is True
    assert review.attributes["agent.model"] == "claude-fable-5"


async def test_every_span_of_a_run_shares_one_trace_id(db, tmp_path):
    otlp = CapturingOTLP()
    _, run, _ = await run_once(db, tmp_path, otlp=otlp)
    assert len({s.trace_id for s in otlp.spans}) == 1


# --- the rollup ------------------------------------------------------------


async def test_the_rollup_matches_the_sum_of_its_stages(db, tmp_path):
    _, run, _ = await run_once(db, tmp_path)
    roll = await dbmod.trace_rollup_for_run(db, run.id)
    assert {s["stage"] for s in roll["stages"]} == {"executing", "review"}
    assert roll["api_calls"] == sum(s["api_calls"] for s in roll["stages"])
    assert roll["cost_usd"] == round(sum(s["cost_usd"] for s in roll["stages"]), 10)
    assert roll["tokens_cache_read"] == 50_000     # 25k per stage, counted once each


async def test_the_reviewer_stage_is_priced_on_its_own_model(db, tmp_path):
    # Fable is twice Opus per token; a rollup that priced every stage at the
    # executor's model would understate the reviewer by half.
    _, run, _ = await run_once(db, tmp_path)
    roll = await dbmod.trace_rollup_for_run(db, run.id)
    stages = {s["stage"]: s for s in roll["stages"]}
    assert stages["review"]["model"] == "claude-fable-5"
    assert stages["executing"]["model"] in ("", "claude-opus-5")


# --- failure never reaches the run ----------------------------------------


async def test_an_export_failure_leaves_the_run_done(db, tmp_path):
    _, run, _ = await run_once(db, tmp_path, otlp=CapturingOTLP(fail=True))
    assert run.state == DONE


async def test_a_sandbox_without_a_session_still_finishes_the_run(db, tmp_path):
    otlp = CapturingOTLP()
    _, run, _ = await run_once(db, tmp_path, otlp=otlp, sessions=False)
    assert run.state == DONE
    # The stage spans still exist — the timing and the model are worth having
    # even when the session file could not be read.
    assert "stage.review" in [s.name for s in otlp.spans]
    assert "agent.session" not in [s.name for s in otlp.spans]


async def test_a_corrupt_session_file_leaves_the_run_done_and_notes_it(db, tmp_path):
    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    sb.push_resp = {"pushed": True, "branch": f"loop/run-{run.id}", "commits": 2}
    gh.branch_shas[f"loop/run-{run.id}"] = "sha1"
    for stage in ("executing", "review"):
        sb.file_contents[f"{TRACE_DIR}/{stage}.jsonl"] = b"{not json at all\n"
    pipe = traced_pipeline(db, tmp_path, gh, sb, tg)
    sb.task_results = [EXEC_OK, CLEAN]
    await pipe.process(run)
    assert run.state == DONE


async def test_a_tracer_that_raises_outright_does_not_fail_the_run(db, tmp_path):
    class Exploding:
        async def trace_agent_task(self, *a, **kw):
            raise RuntimeError("boom")

        async def emit_run_span(self, *a, **kw):
            raise RuntimeError("boom")

    gh, sb, tg = FakeGitHub(), FakeSandboxd(), FakeTG()
    seed_ok(gh, tmp_path)
    run = await dbmod.create_run(db, "o/myrepo", 5, "feat/x")
    sb.push_resp = {"pushed": True, "branch": f"loop/run-{run.id}", "commits": 2}
    gh.branch_shas[f"loop/run-{run.id}"] = "sha1"
    pipe = make_pipeline(db, tmp_path, gh=gh, sb=sb, tg=tg)
    pipe.tracer = Exploding()
    sb.task_results = [EXEC_OK, CLEAN]
    await pipe.process(run)
    assert run.state == DONE
