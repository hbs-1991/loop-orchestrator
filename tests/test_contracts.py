"""The contract protocol: verdict parsing and the renderings that carry it."""
import pytest

from loop_orchestrator import db as dbmod
from loop_orchestrator import issue_tasks as it
from loop_orchestrator.contracts import (
    COMMENT_END_MARKER,
    COMMENT_MARKER,
    Contract,
    ContractError,
    Upstream,
    build_contract_prompt,
    collect_upstreams,
    extract_contract,
    fetch_context_files,
    missing_source_note,
    parse_contract_output,
    render_context_readme,
    render_contract_comment,
    render_upstream_section,
)

from tests.conftest import FakeGitHub


def test_parse_contract():
    c = parse_contract_output(
        'Here is the result: {"outcome": "contract", "contract": "### GET /v1/x",'
        ' "sources": ["/src/api.py", " "], "breaking_changes": ["drops v0"]}')
    assert c.outcome == "contract"
    assert c.contract == "### GET /v1/x"
    assert c.sources == ["src/api.py"]          # leading slash and blanks dropped
    assert c.breaking_changes == ["drops v0"]


def test_parse_none_outcome_needs_no_body():
    c = parse_contract_output('{"outcome": "none", "contract": "", "sources": []}')
    assert c.outcome == "none" and c.contract == "" and c.sources == []


def test_parse_survives_prose_json_before_the_verdict():
    # Regression: the agent quoting `{op: ...}` in prose used to win a greedy match.
    c = parse_contract_output(
        'I considered `{"op": "noop"}` first.\n'
        '{"outcome": "contract", "contract": "POST /v1/y", "sources": []}')
    assert c.contract == "POST /v1/y"


def test_parse_caps_sources_at_ten():
    c = parse_contract_output(
        '{"outcome": "contract", "contract": "x", "sources": %s}'
        % str([f"f{i}.py" for i in range(15)]).replace("'", '"'))
    assert len(c.sources) == 10


@pytest.mark.parametrize("text", [
    "no json here",
    '{"outcome": "maybe"}',
    '{"outcome": "contract", "contract": "   ", "sources": []}',
])
def test_parse_rejects_bad_input(text):
    with pytest.raises(ContractError):
        parse_contract_output(text)


def test_prompt_names_the_diff_and_forbids_writing():
    p = build_contract_prompt("feat/x")
    assert "origin/feat/x..HEAD" in p
    assert "Do NOT modify, commit or push anything" in p
    assert "at most 10 repo-relative paths" in p
    assert '"outcome": "contract | none"' in p


def test_comment_round_trips_through_the_markers():
    body = render_contract_comment(
        Contract(outcome="contract", contract="### GET /v1/x",
                 sources=["src/api.py"], breaking_changes=["drops v0"]),
        pr_number=45, head_sha="abcdef1234")
    assert body.startswith(COMMENT_MARKER)
    assert COMMENT_END_MARKER in body
    assert "PR #45" in body and "abcdef1" in body
    assert "drops v0" in body and "`src/api.py`" in body
    assert extract_contract(body) == "### GET /v1/x"


def test_extract_falls_back_to_the_whole_body():
    assert extract_contract(f"{COMMENT_MARKER}\nhand written\n") == "hand written"


def test_upstream_section_marks_the_contract_authoritative():
    s = render_upstream_section([Upstream(
        repo="o/backend", number=12, title="Ingest API", pr_number=45,
        contract_md="### POST /v1/ingest", sources=["src/api.py"])])
    assert "## Upstream dependencies" in s
    assert "### o/backend#12 — Ingest API (PR #45)" in s
    assert "do not invent endpoints" in s
    assert "### POST /v1/ingest" in s
    assert "`.loop/context/o/backend/`" in s and "- `src/api.py`" in s


def test_upstream_section_without_a_contract_says_so():
    s = render_upstream_section([Upstream(repo="o/backend", number=12, pr_number=45)])
    assert "No contract digest was captured" in s
    assert "do not invent endpoints" not in s


def test_upstream_section_is_empty_without_dependencies():
    assert render_upstream_section([]) == ""


def test_context_readme_lists_producers_and_drops():
    r = render_context_readme(
        [Upstream(repo="o/backend", number=12, sources=["src/api.py"])],
        dropped=["o/backend/src/huge.py"])
    assert "o/backend#12" in r and "o/backend/src/huge.py" in r


def test_missing_source_note_explains_itself():
    n = missing_source_note("o/backend", "main", "src/gone.py")
    assert "src/gone.py" in n and "main" in n and "o/backend" in n


async def test_collect_prefers_the_edited_comment_over_the_stored_row(db):
    gh = FakeGitHub()
    await dbmod.save_contract(db, "o/backend", 12, run_id=7, pr_number=45,
                              head_sha="abc", contract_md="### machine wrote this",
                              sources=["src/api.py"], breaking=[])
    gh.issue_comments[12] = [{"id": 1, "body":
                              "<!-- loop:api-contract -->\n### a human fixed it\n"
                              "<!-- /loop:api-contract -->\nfooter"}]
    gh.issues[12] = {"number": 12, "title": "Ingest API", "state": "closed"}
    await it.upsert_task(db, "o/frontend", 13, "F", None)
    await it.set_depends_on(db, "o/frontend", 13,
                            [{"repo": "o/backend", "number": 12}])
    task = await it.get_task(db, "o/frontend", 13)

    [u] = await collect_upstreams(db, gh, task)
    assert u.contract_md == "### a human fixed it"
    assert u.sources == ["src/api.py"]     # sources still come from the row
    assert u.title == "Ingest API" and u.pr_number == 45


async def test_collect_falls_back_to_the_blocker_pr_when_no_contract(db):
    gh = FakeGitHub()
    gh.issues[12] = {"number": 12, "title": "Ingest API", "state": "closed"}
    run = await dbmod.create_run(db, "o/backend", 45, "loop/issue-12")
    run.issue_number = 12
    await dbmod.save_run(db, run)
    await it.upsert_task(db, "o/frontend", 13, "F", None)
    await it.set_depends_on(db, "o/frontend", 13,
                            [{"repo": "o/backend", "number": 12}])
    task = await it.get_task(db, "o/frontend", 13)

    [u] = await collect_upstreams(db, gh, task)
    assert u.contract_md == "" and u.pr_number == 45 and u.sources == []


async def test_fetch_places_files_and_notes_a_missing_one():
    gh = FakeGitHub()
    gh.files["src/api.py"] = "print('real')"
    files, dropped = await fetch_context_files(
        gh, [Upstream(repo="o/backend", number=12,
                      sources=["src/api.py", "src/gone.py"])])
    assert files[".loop/context/o/backend/src/api.py"] == "print('real')"
    assert "does not exist" in files[".loop/context/o/backend/src/gone.py"]
    assert dropped == []


async def test_fetch_drops_what_exceeds_the_budget():
    gh = FakeGitHub()
    gh.files["big.py"] = "x" * (300 * 1024)
    files, dropped = await fetch_context_files(
        gh, [Upstream(repo="o/backend", number=12, sources=["big.py"])])
    assert files == {} and dropped == ["o/backend/big.py"]
