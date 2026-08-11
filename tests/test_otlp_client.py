import json

import httpx
import respx

from loop_orchestrator.clients.otlp import OTLPClient, encode_spans
from loop_orchestrator.tracing.model import Span

EP = "http://jaeger:4318"
TRACE = "a" * 32


def make_client():
    return OTLPClient(EP, service_name="loop-orchestrator")


def _spans():
    root = Span(name="run #1", trace_id=TRACE, span_id="1" * 16,
                start_ns=1_000_000_000, end_ns=5_000_000_000)
    root.set(**{"run.id": 1, "cost.usd": 1.25, "session.fresh": True, "repo": "o/r"})
    child = Span(name="api.call #1", trace_id=TRACE, span_id="2" * 16,
                 parent_id=root.span_id, start_ns=2_000_000_000,
                 end_ns=3_000_000_000)
    child.fail("tool returned an error")
    return [root, child]


def test_payload_has_the_otlp_envelope():
    payload = encode_spans(_spans(), "loop-orchestrator")
    resource = payload["resourceSpans"][0]
    assert resource["resource"]["attributes"][0] == {
        "key": "service.name", "value": {"stringValue": "loop-orchestrator"}}
    assert len(resource["scopeSpans"][0]["spans"]) == 2


def test_parent_is_present_on_children_and_absent_on_the_root():
    spans = encode_spans(_spans(), "svc")["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert "parentSpanId" not in spans[0]
    assert spans[1]["parentSpanId"] == "1" * 16


def test_timestamps_are_strings():
    # A nanosecond timestamp does not survive a JSON number: OTLP/JSON requires
    # 64-bit values to be sent as strings.
    span = encode_spans(_spans(), "svc")["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["startTimeUnixNano"] == "1000000000"
    assert isinstance(span["endTimeUnixNano"], str)


def test_each_attribute_type_gets_its_own_variant():
    span = encode_spans(_spans(), "svc")["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    attrs = {a["key"]: a["value"] for a in span["attributes"]}
    assert attrs["run.id"] == {"intValue": "1"}
    assert attrs["cost.usd"] == {"doubleValue": 1.25}
    # bool IS an int in Python; encoding True as intValue would lose it.
    assert attrs["session.fresh"] == {"boolValue": True}
    assert attrs["repo"] == {"stringValue": "o/r"}


def test_error_status_carries_its_message():
    spans = encode_spans(_spans(), "svc")["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[0]["status"] == {"code": 1}
    assert spans[1]["status"] == {"code": 2, "message": "tool returned an error"}


@respx.mock
async def test_export_posts_to_v1_traces():
    route = respx.post(f"{EP}/v1/traces").mock(return_value=httpx.Response(200))
    assert await make_client().export(_spans()) is True
    body = json.loads(route.calls[0].request.content)
    assert body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] == TRACE


@respx.mock
async def test_an_empty_batch_is_not_posted():
    route = respx.post(f"{EP}/v1/traces")
    assert await make_client().export([]) is True
    assert not route.called


@respx.mock
async def test_a_server_error_is_retried_then_succeeds():
    route = respx.post(f"{EP}/v1/traces").mock(side_effect=[
        httpx.Response(503), httpx.Response(200)])
    client = OTLPClient(EP, client=httpx.AsyncClient())
    assert await client.export(_spans()) is True
    assert route.call_count == 2


@respx.mock
async def test_an_unreachable_collector_returns_false_and_never_raises():
    # Losing a trace must never fail a run.
    respx.post(f"{EP}/v1/traces").mock(side_effect=httpx.ConnectError("down"))
    assert await OTLPClient(EP, client=httpx.AsyncClient()).export(_spans()) is False


@respx.mock
async def test_a_4xx_returns_false_without_retrying():
    route = respx.post(f"{EP}/v1/traces").mock(return_value=httpx.Response(400))
    assert await OTLPClient(EP, client=httpx.AsyncClient()).export(_spans()) is False
    assert route.call_count == 1


def test_endpoint_trailing_slash_is_normalised():
    assert OTLPClient("http://x:4318/").endpoint == "http://x:4318"
