from loop_orchestrator.jsonextract import find_json_object


def test_bare_object():
    assert find_json_object('{"verdict": "clean"}') == {"verdict": "clean"}


def test_prose_braces_before_fenced_verdict():
    # Live repro (run #20): the advisor's analysis mentions `{op: ...}` in
    # prose before the fenced verdict; a greedy regex chokes on this.
    text = ('The e2e tests post `{op: ...}` directly and never inspect it.\n'
            'Verdict follows:\n```json\n{"verdict": "approved", '
            '"summary": "ok", "issues": []}\n```')
    assert find_json_object(text, "verdict") == {
        "verdict": "approved", "summary": "ok", "issues": []}


def test_prefers_object_with_expected_key():
    text = ('{"note": "schema example"}\ndone\n'
            '{"outcome": "plan", "summary": "s"}\n{"unrelated": 1}')
    assert find_json_object(text, "outcome") == {
        "outcome": "plan", "summary": "s"}


def test_last_object_wins_without_preference():
    assert find_json_object('{"a": 1} then {"b": 2}') == {"b": 2}


def test_none_when_no_valid_object():
    assert find_json_object("no json here, only {braces} and {more") is None
    assert find_json_object("") is None
    assert find_json_object("[1, 2, 3]") is None  # arrays are not verdicts
