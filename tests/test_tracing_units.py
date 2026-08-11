import json

import pytest

from loop_orchestrator.tracing.model import Span, new_span_id, trace_id_for_run
from loop_orchestrator.tracing.pricing import PRICES, cost_usd, load_overrides
from loop_orchestrator.tracing.redact import Redactor

# --- model -----------------------------------------------------------------


def test_ids_have_the_widths_otlp_requires():
    assert len(new_span_id()) == 16
    assert len(trace_id_for_run(1)) == 32
    int(new_span_id(), 16)          # hex, or this raises
    int(trace_id_for_run(1), 16)


def test_trace_id_is_stable_per_run():
    # A Run outlives the process: recovery after a restart and a revise hours
    # later must land in the SAME trace, not start a second one.
    assert trace_id_for_run(42) == trace_id_for_run(42)
    assert trace_id_for_run(42) != trace_id_for_run(43)


def test_span_set_drops_empty_values():
    s = Span(name="x", trace_id="t").set(a=1, b=None, c="", d=False, e=0)
    # None and "" are "we did not learn this"; False and 0 are measurements.
    assert s.attributes == {"a": 1, "d": False, "e": 0}


def test_span_duration_and_failure():
    s = Span(name="x", trace_id="t", start_ns=1_000_000, end_ns=3_000_000)
    assert s.duration_ms == 2.0
    assert s.fail("boom").status == "error" and s.error_message == "boom"


# --- pricing ---------------------------------------------------------------


def test_each_token_kind_is_priced_at_its_own_rate():
    usage = {"input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000,
             "cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000}
    usd, priced = cost_usd("claude-opus-5", usage)
    assert priced
    assert usd == pytest.approx(5.0 + 6.25 + 0.5 + 25.0)


def test_fable_is_twice_opus():
    # Not a trivia test: the reviewer runs on fable, and assuming it was the
    # cheap model is what made the first cost analysis draw the wrong conclusion.
    usage = {"output_tokens": 1_000_000}
    assert cost_usd("claude-fable-5", usage)[0] == 2 * cost_usd("claude-opus-5", usage)[0]


def test_unknown_model_is_reported_unpriced_not_free():
    assert cost_usd("some-new-model", {"output_tokens": 999}) == (0.0, False)


def test_overrides_replace_one_model_and_keep_the_rest():
    table = load_overrides(json.dumps({"claude-opus-5": {
        "input": 1, "cache_write": 2, "cache_read": 3, "output": 4}}))
    assert table["claude-opus-5"].output == 4
    assert table["claude-fable-5"] == PRICES["claude-fable-5"]


@pytest.mark.parametrize("raw", ["not json", "[1,2]", '{"m": "nope"}',
                                 '{"m": {"input": 1}}', ""])
def test_malformed_overrides_are_ignored_not_raised(raw):
    # A typo in an env var must not stop the orchestrator from starting.
    assert load_overrides(raw)["claude-opus-5"] == PRICES["claude-opus-5"]


# --- redaction -------------------------------------------------------------


def test_secret_is_removed_from_a_preview():
    r = Redactor(["hunter2000"])
    assert r.preview("psql -p hunter2000 db", 100) == "psql -p *** db"


def test_secret_is_removed_before_truncation():
    # Truncating first can cut a credential in half and leave the first half in
    # the span. Visibly a fragment; still a fragment of a credential.
    r = Redactor(["SUPERSECRETVALUE"])
    out = r.preview("x" * 20 + "SUPERSECRETVALUE" + "y" * 100, 30)
    assert "SUPERSECRET" not in out and "SECRETVALUE" not in out


def test_longest_secret_is_masked_first():
    r = Redactor(["abcd", "abcdefgh"])
    assert r.preview("token=abcdefgh", 100) == "token=***"


def test_short_values_are_not_treated_as_secrets():
    r = Redactor(["1", "ab"])
    assert r.preview("run 1 of ab", 100) == "run 1 of ab"


def test_preview_collapses_whitespace_and_respects_the_limit():
    r = Redactor([])
    assert r.preview("a\n\n  b\tc", 100) == "a b c"
    out = r.preview("x" * 50, 10)
    assert out == "x" * 10 + "..." and len(out) == 13


def test_preview_of_nothing_is_empty():
    r = Redactor(["s3cret"])
    assert r.preview(None, 10) == "" and r.preview("   ", 10) == ""
