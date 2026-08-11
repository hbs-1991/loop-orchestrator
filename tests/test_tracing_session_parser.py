"""The parser turns one session JSONL into the span subtree.

The fixture is built inline rather than checked in as a file: every quirk it
exercises is a real property of Claude Code's format, and spelling them out here
means a reader sees WHY each line is shaped the way it is.
"""
import json

from loop_orchestrator.tracing.redact import Redactor
from loop_orchestrator.tracing.session_parser import parse_session

R = Redactor([])


def _assistant(mid, ts, usage, tools=(), model="claude-opus-5"):
    return {"type": "assistant", "timestamp": ts, "sessionId": "s-1",
            "message": {"id": mid, "model": model, "usage": usage,
                        "content": [{"type": "tool_use", "id": t[0],
                                     "name": t[1], "input": t[2]} for t in tools]}}


def _result(tid, text, ts, is_error=False):
    return {"type": "user", "timestamp": ts,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                     "content": text, "is_error": is_error}]}}


def _lines(*entries):
    return "\n".join(json.dumps(e) for e in entries).encode()


USAGE1 = {"input_tokens": 4, "cache_creation_input_tokens": 20_000,
          "cache_read_input_tokens": 25_000, "output_tokens": 300}
USAGE2 = {"input_tokens": 2, "cache_creation_input_tokens": 5_000,
          "cache_read_input_tokens": 45_004, "output_tokens": 100}


def test_one_api_response_split_over_several_lines_is_one_call():
    # THE trap. Claude Code writes one API response as up to six JSONL lines
    # sharing message.id, each repeating the SAME usage. Counting lines reported
    # 397 calls where there were 209, and doubled every dollar.
    raw = _lines(
        _assistant("msg_1", "2026-08-06T10:00:00Z", USAGE1, [("t1", "Read", {"file_path": "a.py"})]),
        _assistant("msg_1", "2026-08-06T10:00:02Z", USAGE1, [("t2", "Bash", {"command": "ls"})]),
        _result("t1", "file body", "2026-08-06T10:00:03Z"),
        _result("t2", "a.py b.py", "2026-08-06T10:00:03Z"),
    )
    t = parse_session(raw, redactor=R, trace_id="t" * 32)
    assert t.api_calls == 1
    assert t.tool_calls == 2                       # both tools kept
    assert t.tokens["cache_read"] == 25_000        # usage counted ONCE
    assert t.session.attributes["session.api_calls"] == 1


def test_context_growth_is_attributed_call_by_call():
    raw = _lines(
        _assistant("m1", "2026-08-06T10:00:00Z", USAGE1),
        _assistant("m2", "2026-08-06T10:00:10Z", USAGE2),
    )
    t = parse_session(raw, redactor=R, trace_id="t" * 32)
    calls = [s for s in t.spans if s.name.startswith("api.call")]
    assert calls[0].attributes["context.tokens"] == 45_004
    assert calls[0].attributes["context.delta"] == 45_004   # the opening context
    assert calls[1].attributes["context.tokens"] == 50_006
    assert calls[1].attributes["context.delta"] == 5_002


def test_opening_context_is_recorded_on_the_session():
    # "What does a fresh session start with" — the number is call #1's context,
    # before the agent has done anything: system prompt + tools + our prompt.
    raw = _lines(_assistant("m1", "2026-08-06T10:00:00Z", USAGE1))
    t = parse_session(raw, redactor=R, trace_id="t" * 32)
    assert t.session.attributes["session.opening_context_tokens"] == 45_004


def test_cache_miss_is_flagged_after_the_first_call():
    miss = {"input_tokens": 3, "cache_creation_input_tokens": 60_000,
            "cache_read_input_tokens": 0, "output_tokens": 50}
    raw = _lines(
        _assistant("m1", "2026-08-06T10:00:00Z", USAGE1),
        _assistant("m2", "2026-08-06T10:00:05Z", miss),
    )
    calls = [s for s in parse_session(raw, redactor=R, trace_id="t" * 32).spans
             if s.name.startswith("api.call")]
    assert "cache.miss" not in calls[0].attributes    # call #1 always misses
    assert calls[1].attributes["cache.miss"] is True


def test_an_idle_gap_past_the_cache_ttl_is_flagged():
    raw = _lines(
        _assistant("m1", "2026-08-06T10:00:00Z", USAGE1),
        _assistant("m2", "2026-08-06T10:10:05Z", USAGE2),   # 10 minutes later
    )
    call2 = [s for s in parse_session(raw, redactor=R, trace_id="t" * 32).spans
             if s.name == "api.call #2"][0]
    assert call2.attributes["idle_before_s"] == 605.0
    assert call2.attributes["cache.expired_while_idle"] is True


def test_a_short_gap_is_not_flagged():
    raw = _lines(
        _assistant("m1", "2026-08-06T10:00:00Z", USAGE1),
        _assistant("m2", "2026-08-06T10:00:30Z", USAGE2),
    )
    call2 = [s for s in parse_session(raw, redactor=R, trace_id="t" * 32).spans
             if s.name == "api.call #2"][0]
    assert "cache.expired_while_idle" not in call2.attributes


def test_tool_results_are_matched_and_previewed():
    raw = _lines(
        _assistant("m1", "2026-08-06T10:00:00Z", USAGE1,
                   [("t1", "Bash", {"command": "npm test"})]),
        _result("t1", "PASS " * 400, "2026-08-06T10:00:04Z"),
    )
    tool = [s for s in parse_session(raw, redactor=R, preview_chars=50,
                                     trace_id="t" * 32).spans
            if s.name.startswith("tool.")][0]
    assert tool.name == "tool.Bash"
    assert tool.attributes["tool.args"] == '{"command": "npm test"}'
    assert tool.attributes["result.chars"] == 2000
    assert len(tool.attributes["result.preview"]) == 53      # 50 + "..."


def test_a_failed_tool_marks_the_span_and_the_session():
    raw = _lines(
        _assistant("m1", "2026-08-06T10:00:00Z", USAGE1,
                   [("t1", "Bash", {"command": "false"})]),
        _result("t1", "exit 1", "2026-08-06T10:00:01Z", is_error=True),
    )
    t = parse_session(raw, redactor=R, trace_id="t" * 32)
    tool = [s for s in t.spans if s.name.startswith("tool.")][0]
    assert tool.attributes["tool.error"] is True and tool.status == "error"
    assert t.session.attributes["session.had_tool_errors"] is True


def test_secrets_never_reach_a_preview():
    raw = _lines(
        _assistant("m1", "2026-08-06T10:00:00Z", USAGE1,
                   [("t1", "Bash", {"command": "curl -H 'key: sk-live-abcdef123'"})]),
        _result("t1", "logged in as sk-live-abcdef123", "2026-08-06T10:00:01Z"),
    )
    tool = [s for s in parse_session(raw, redactor=Redactor(["sk-live-abcdef123"]),
                                     trace_id="t" * 32).spans
            if s.name.startswith("tool.")][0]
    assert "sk-live-abcdef123" not in tool.attributes["tool.args"]
    assert "sk-live-abcdef123" not in tool.attributes["result.preview"]
    assert "***" in tool.attributes["result.preview"]


def test_a_malformed_line_is_skipped_not_raised():
    # The file is copied out of a sandbox that may have been killed mid-write.
    raw = (json.dumps(_assistant("m1", "2026-08-06T10:00:00Z", USAGE1)).encode()
           + b"\n{\"type\": \"assistant\", \"messa\n"
           + json.dumps(_assistant("m2", "2026-08-06T10:00:05Z", USAGE2)).encode())
    t = parse_session(raw, redactor=R, trace_id="t" * 32)
    assert t.api_calls == 2


def test_empty_or_contentless_input_returns_none():
    assert parse_session(b"", redactor=R) is None
    assert parse_session(b"\n\n", redactor=R) is None
    assert parse_session(json.dumps({"type": "user"}).encode(), redactor=R) is None


def test_the_fresh_flag_comes_from_the_caller_not_the_file():
    # It reflects the `continue` we sent to sandboxd. The file cannot know it.
    raw = _lines(_assistant("m1", "2026-08-06T10:00:00Z", USAGE1))
    assert parse_session(raw, redactor=R, fresh=False).session.attributes[
        "session.fresh"] is False
    assert parse_session(raw, redactor=R, fresh=True).session.attributes[
        "session.fresh"] is True
    assert "session.fresh" not in parse_session(raw, redactor=R).session.attributes


def test_unpriced_model_is_marked_rather_than_shown_as_free():
    raw = _lines(_assistant("m1", "2026-08-06T10:00:00Z", USAGE1,
                            model="claude-unknown-9"))
    t = parse_session(raw, redactor=R, trace_id="t" * 32)
    assert t.session.attributes["cost.unpriced"] is True
    assert t.cost == 0.0


def test_spans_form_one_tree_under_the_session():
    raw = _lines(
        _assistant("m1", "2026-08-06T10:00:00Z", USAGE1,
                   [("t1", "Read", {"file_path": "a"})]),
        _result("t1", "body", "2026-08-06T10:00:01Z"),
    )
    t = parse_session(raw, redactor=R, trace_id="t" * 32, parent_id="p" * 16)
    by_id = {s.span_id: s for s in t.spans}
    assert t.session.parent_id == "p" * 16
    call = [s for s in t.spans if s.name.startswith("api.call")][0]
    tool = [s for s in t.spans if s.name.startswith("tool.")][0]
    assert call.parent_id == t.session.span_id
    assert tool.parent_id == call.span_id
    assert all(s.trace_id == "t" * 32 for s in t.spans)
    assert by_id[tool.parent_id] is call
