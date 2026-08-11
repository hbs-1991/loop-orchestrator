"""One Claude Code session JSONL -> a span subtree.

    agent.session
    └── api.call #n
        └── tool.<Name>

The file is read out of a sandbox that may have been killed mid-write, so every
line is parsed defensively: a bad line is skipped, never raised.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime

from .model import ERROR, Span, new_span_id
from .pricing import Price, cost_usd

# The prompt cache expires after five minutes of inactivity; past that the whole
# context is re-billed at write price on the next call. An idle gap wider than
# this is the single most expensive thing an agent can do, so it is marked.
CACHE_TTL_S = 300


@dataclass
class SessionTrace:
    session: Span
    spans: list[Span] = field(default_factory=list)  # session + descendants
    api_calls: int = 0
    tool_calls: int = 0
    tokens: dict = field(default_factory=dict)
    cost: float = 0.0
    model: str = ""


def _ts_ns(value) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp() * 1e9)
    except (ValueError, TypeError):
        return 0


def _blocks(message) -> list:
    content = (message or {}).get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _load(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # truncated or corrupt line — the sandbox may have died
        if isinstance(entry, dict):
            out.append(entry)
    return out


def parse_session(raw: bytes, *, redactor, preview_chars: int = 500,
                  trace_id: str = "", parent_id: str | None = None,
                  fresh: bool | None = None, stage: str = "",
                  prices: dict[str, Price] | None = None,
                  cache_ttl_s: int = CACHE_TTL_S) -> SessionTrace | None:
    entries = _load(raw)
    if not entries:
        return None

    # tool_use_id -> what came back
    results: dict[str, tuple[int, str, bool]] = {}
    for e in entries:
        if e.get("type") != "user":
            continue
        for b in _blocks(e.get("message")):
            if b.get("type") == "tool_result":
                text = _result_text(b)
                results[b.get("tool_use_id")] = (
                    len(text), redactor.preview(text, preview_chars),
                    bool(b.get("is_error")))

    # An API response is written as SEVERAL assistant lines sharing message.id,
    # each repeating the SAME usage block. Counting lines double-counts every
    # token and every dollar: the first hand-written profiler reported 397 calls
    # where there were 209. Deduplication here is a correctness requirement.
    calls: dict[str, dict] = {}
    order: list[str] = []
    session_id = ""
    opening = ""
    for e in entries:
        session_id = session_id or str(e.get("sessionId") or "")
        if e.get("type") == "user" and not opening:
            text = "".join(b.get("text", "") for b in _blocks(e.get("message"))
                           if b.get("type") == "text")
            if text.strip():
                opening = text
        if e.get("type") != "assistant":
            continue
        message = e.get("message") or {}
        mid = str(message.get("id") or e.get("uuid") or len(order))
        ns = _ts_ns(e.get("timestamp"))
        if mid not in calls:
            calls[mid] = {"model": message.get("model") or "",
                          "usage": message.get("usage") or {},
                          "tools": [], "start_ns": ns, "end_ns": ns}
            order.append(mid)
        call = calls[mid]
        call["end_ns"] = max(call["end_ns"], ns)
        if not call["usage"] and message.get("usage"):
            call["usage"] = message["usage"]
        seen = {t.get("id") for t in call["tools"]}
        for b in _blocks(message):
            if b.get("type") == "tool_use" and b.get("id") not in seen:
                call["tools"].append(b)
                seen.add(b.get("id"))

    if not order:
        return None

    model = calls[order[0]]["model"]
    session = Span(name="agent.session", trace_id=trace_id, parent_id=parent_id,
                   span_id=new_span_id(),
                   start_ns=calls[order[0]]["start_ns"],
                   end_ns=calls[order[-1]]["end_ns"])
    spans = [session]
    totals = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    spend = 0.0
    unpriced = False
    tool_calls = 0
    prev_ctx = 0
    prev_end_ns = 0

    for i, mid in enumerate(order, start=1):
        call = calls[mid]
        u = call["usage"]
        inp = u.get("input_tokens") or 0
        cw = u.get("cache_creation_input_tokens") or 0
        cr = u.get("cache_read_input_tokens") or 0
        out = u.get("output_tokens") or 0
        ctx = inp + cw + cr
        # A context smaller than the previous one is a reset (compaction, or a
        # fresh session inside the same file), not a negative delta.
        delta = ctx - prev_ctx if ctx >= prev_ctx else ctx
        usd, priced = cost_usd(call["model"], u, prices)
        unpriced = unpriced or not priced

        span = Span(name=f"api.call #{i}", trace_id=trace_id,
                    parent_id=session.span_id, span_id=new_span_id(),
                    start_ns=call["start_ns"], end_ns=call["end_ns"])
        span.set(**{
            "agent.model": call["model"],
            "context.tokens": ctx,
            "context.delta": delta,
            "tokens.input": inp,
            "tokens.cache_write": cw,
            "tokens.cache_read": cr,
            "tokens.output": out,
            "cost.usd": round(usd, 6),
            "call.index": i,
        })
        if not priced:
            span.set(**{"cost.unpriced": True})
        if i > 1 and cr == 0:
            # Nothing was reused: the entire context was re-written at 1.25x.
            span.set(**{"cache.miss": True})
        if i > 1 and prev_end_ns and call["start_ns"] > prev_end_ns:
            idle = (call["start_ns"] - prev_end_ns) / 1e9
            span.set(**{"idle_before_s": round(idle, 1)})
            if idle > cache_ttl_s:
                span.set(**{"cache.expired_while_idle": True})
        spans.append(span)

        for t in call["tools"]:
            chars, prev, is_err = results.get(t.get("id"), (0, "", False))
            try:
                args = json.dumps(t.get("input") or {}, ensure_ascii=False)
            except (TypeError, ValueError):
                args = str(t.get("input"))
            tool = Span(name=f"tool.{t.get('name') or 'unknown'}",
                        trace_id=trace_id, parent_id=span.span_id,
                        span_id=new_span_id(),
                        start_ns=call["end_ns"], end_ns=call["end_ns"])
            tool.set(**{
                "tool.name": t.get("name") or "unknown",
                "tool.args": redactor.preview(args, preview_chars),
                "result.chars": chars,
                "result.preview": prev,
            })
            if is_err:
                tool.set(**{"tool.error": True})
                tool.fail("tool returned an error")
            spans.append(tool)
            tool_calls += 1

        totals["input"] += inp
        totals["cache_write"] += cw
        totals["cache_read"] += cr
        totals["output"] += out
        spend += usd
        prev_ctx = ctx
        prev_end_ns = call["end_ns"]

    session.set(**{
        "session.id": session_id,
        "agent.model": model,
        "session.api_calls": len(order),
        "session.tool_calls": tool_calls,
        "tokens.input": totals["input"],
        "tokens.cache_write": totals["cache_write"],
        "tokens.cache_read": totals["cache_read"],
        "tokens.output": totals["output"],
        "cost.usd": round(spend, 6),
        "prompt.chars": len(opening),
        "prompt.preview": redactor.preview(opening, preview_chars),
        "loop.stage": stage,
        # The context the session opened with, before the agent did anything:
        # system prompt + tool definitions + our stage prompt.
        "session.opening_context_tokens": spans[1].attributes.get("context.tokens", 0),
    })
    if fresh is not None:
        # Recorded from the `continue` we actually sent, not inferred from the
        # file — the whole point is to see whether a stage started clean.
        session.set(**{"session.fresh": fresh})
    if unpriced:
        session.set(**{"cost.unpriced": True})
    if any(s.status == ERROR for s in spans):
        session.set(**{"session.had_tool_errors": True})

    return SessionTrace(session=session, spans=spans, api_calls=len(order),
                        tool_calls=tool_calls, tokens=totals, cost=spend,
                        model=model)
