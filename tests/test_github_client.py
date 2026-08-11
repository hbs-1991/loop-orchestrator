import base64
import json

import httpx
import pytest
import respx

from loop_orchestrator.clients.github import (
    LOOP_LABELS,
    FastForwardError,
    GitHubClient,
    MergeError,
)

GH = "https://api.github.com"


def test_needs_review_label_registered():
    assert LOOP_LABELS["loop:needs-review"] == "e4e669"


@respx.mock
async def test_get_file_found_and_missing():
    respx.get(f"{GH}/repos/o/r/contents/.loop.yml").mock(return_value=httpx.Response(
        200, json={"content": base64.b64encode("specs_dir: d".encode()).decode()}))
    respx.get(f"{GH}/repos/o/r/contents/nope.yml").mock(return_value=httpx.Response(404))
    gh = GitHubClient("tok")
    assert await gh.get_file("o/r", "br", ".loop.yml") == "specs_dir: d"
    assert await gh.get_file("o/r", "br", "nope.yml") is None


@respx.mock
async def test_list_pr_files_paginates():
    page1 = [{"filename": f"f{i}.py"} for i in range(100)]
    page2 = [{"filename": "last.py"}]
    route = respx.get(f"{GH}/repos/o/r/pulls/5/files").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)])
    files = await GitHubClient("tok").list_pr_files("o/r", 5)
    assert len(files) == 101 and files[-1] == "last.py"
    assert route.call_count == 2


@respx.mock
async def test_labels_and_comment():
    respx.post(f"{GH}/repos/o/r/labels").mock(return_value=httpx.Response(422))
    add = respx.post(f"{GH}/repos/o/r/issues/5/labels").mock(return_value=httpx.Response(200, json=[]))
    rem = respx.delete(f"{GH}/repos/o/r/issues/5/labels/loop%3Arun").mock(return_value=httpx.Response(404))
    com = respx.post(f"{GH}/repos/o/r/issues/5/comments").mock(return_value=httpx.Response(201, json={}))
    gh = GitHubClient("tok")
    await gh.ensure_labels("o/r")          # a 422 does not blow up
    await gh.add_labels("o/r", 5, ["loop:running"])
    await gh.remove_label("o/r", 5, "loop:run")   # a 404 does not blow up
    await gh.create_comment("o/r", 5, "hi")
    assert add.called and rem.called and com.called


@respx.mock
async def test_get_pr_and_update_branch():
    respx.get(f"{GH}/repos/o/r/pulls/5").mock(return_value=httpx.Response(
        200, json={"mergeable": False, "mergeable_state": "dirty",
                   "base": {"ref": "main"}}))
    upd = respx.put(f"{GH}/repos/o/r/pulls/5/update-branch").mock(
        return_value=httpx.Response(202, json={"message": "Updating"}))
    gh = GitHubClient("tok")
    pr = await gh.get_pr("o/r", 5)
    assert pr["mergeable"] is False and pr["base"]["ref"] == "main"
    await gh.update_pr_branch("o/r", 5)
    assert upd.called


@respx.mock
async def test_list_check_runs():
    respx.get(f"{GH}/repos/o/r/commits/abc/check-runs").mock(
        return_value=httpx.Response(200, json={"check_runs": [
            {"name": "ci", "status": "completed", "conclusion": "failure"}]}))
    runs = await GitHubClient("tok").list_check_runs("o/r", "abc")
    assert runs == [{"name": "ci", "status": "completed",
                     "conclusion": "failure"}]


@respx.mock
async def test_required_checks_reads_the_branch_ruleset():
    respx.get(f"{GH}/repos/o/r/rules/branches/main").mock(
        return_value=httpx.Response(200, json=[
            {"type": "pull_request", "parameters": {}},
            {"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "ci"},
                                           {"context": "Build"}]}},
        ]))
    assert await GitHubClient("tok").required_checks("o/r", "main") == ["ci", "Build"]


@respx.mock
async def test_required_checks_of_an_unreadable_branch_is_empty():
    # A token without the permission, or a plan that does not enforce rules:
    # unknown must not become a reason to refuse a merge.
    respx.get(f"{GH}/repos/o/r/rules/branches/main").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible"}))
    assert await GitHubClient("tok").required_checks("o/r", "main") == []


@respx.mock
async def test_behind_by_counts_what_the_base_has_and_the_head_lacks():
    respx.get(f"{GH}/repos/o/r/compare/main...feat/x").mock(
        return_value=httpx.Response(200, json={"status": "diverged",
                                               "ahead_by": 3, "behind_by": 2}))
    assert await GitHubClient("tok").behind_by("o/r", "main", "feat/x") == 2


@respx.mock
async def test_behind_by_is_zero_when_compare_cannot_be_read():
    # Same stance as required_checks: a diagnostic that fails must not turn
    # into a refusal to merge.
    respx.get(f"{GH}/repos/o/r/compare/main...feat/x").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"}))
    assert await GitHubClient("tok").behind_by("o/r", "main", "feat/x") == 0


@respx.mock
async def test_update_branch_422_raises():
    from loop_orchestrator.clients.github import GitHubError
    respx.put(f"{GH}/repos/o/r/pulls/5/update-branch").mock(
        return_value=httpx.Response(422, json={"message": "merge conflict"}))
    with pytest.raises(GitHubError, match="merge conflict"):
        await GitHubClient("tok").update_pr_branch("o/r", 5)


@respx.mock
async def test_refs():
    respx.get(f"{GH}/repos/o/r/git/ref/heads/loop/run-7").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "abc123"}}))
    ff_ok = respx.patch(f"{GH}/repos/o/r/git/refs/heads/feat").mock(
        return_value=httpx.Response(200, json={}))
    respx.delete(f"{GH}/repos/o/r/git/refs/heads/loop/run-7").mock(return_value=httpx.Response(204))
    gh = GitHubClient("tok")
    assert await gh.branch_sha("o/r", "loop/run-7") == "abc123"
    await gh.fast_forward("o/r", "feat", "abc123")
    import json
    assert json.loads(ff_ok.calls[0].request.content) == {"sha": "abc123", "force": False}
    await gh.delete_branch("o/r", "loop/run-7")


@respx.mock
async def test_fast_forward_conflict():
    respx.patch(f"{GH}/repos/o/r/git/refs/heads/feat").mock(
        return_value=httpx.Response(422, json={"message": "Update is not a fast forward"}))
    with pytest.raises(FastForwardError):
        await GitHubClient("tok").fast_forward("o/r", "feat", "abc123")


@respx.mock
async def test_merge_pr_squash():
    route = respx.put(f"{GH}/repos/o/r/pulls/5/merge").respond(200, json={"merged": True})
    await GitHubClient("tok").merge_pr("o/r", 5, commit_title="feat: x (#5)")
    body = json.loads(route.calls[0].request.content)
    assert body == {"merge_method": "squash", "commit_title": "feat: x (#5)"}


@respx.mock
async def test_merge_pr_without_title_omits_field():
    route = respx.put(f"{GH}/repos/o/r/pulls/5/merge").respond(200, json={"merged": True})
    await GitHubClient("tok").merge_pr("o/r", 5)
    assert json.loads(route.calls[0].request.content) == {"merge_method": "squash"}


@respx.mock
async def test_merge_pr_conflict_raises():
    respx.put(f"{GH}/repos/o/r/pulls/5/merge").respond(
        405, json={"message": "Pull Request is not mergeable"})
    with pytest.raises(MergeError, match="not mergeable"):
        await GitHubClient("tok").merge_pr("o/r", 5)


def test_ready_label_registered():
    assert LOOP_LABELS["loop:ready"] == "5319e7"


@respx.mock
async def test_get_branch_sha_none_on_404():
    respx.get(f"{GH}/repos/o/r/git/ref/heads/loop/issue-7").mock(
        return_value=httpx.Response(404))
    gh = GitHubClient("t")
    assert await gh.get_branch_sha("o/r", "loop/issue-7") is None


@respx.mock
async def test_create_branch_tolerates_existing():
    route = respx.post(f"{GH}/repos/o/r/git/refs").mock(
        return_value=httpx.Response(422))
    gh = GitHubClient("t")
    await gh.create_branch("o/r", "loop/issue-7", "abc")
    assert route.called


@respx.mock
async def test_put_file_updates_with_existing_sha():
    respx.get(f"{GH}/repos/o/r/contents/.loop/task.md").mock(
        return_value=httpx.Response(200, json={"sha": "oldsha", "content": ""}))
    put = respx.put(f"{GH}/repos/o/r/contents/.loop/task.md").mock(
        return_value=httpx.Response(200, json={}))
    gh = GitHubClient("t")
    await gh.put_file("o/r", "loop/issue-7", ".loop/task.md", "body", "msg")
    sent = json.loads(put.calls[0].request.content)
    assert sent["sha"] == "oldsha" and sent["branch"] == "loop/issue-7"


@respx.mock
async def test_create_pr_returns_number():
    respx.post(f"{GH}/repos/o/r/pulls").mock(
        return_value=httpx.Response(201, json={"number": 51}))
    gh = GitHubClient("t")
    assert await gh.create_pr("o/r", "loop/issue-7", "main", "T", "Closes #7.") == 51


@respx.mock
async def test_list_ready_issues_filters_prs():
    respx.get(f"{GH}/repos/o/r/issues").mock(
        return_value=httpx.Response(200, json=[
            {"number": 7, "title": "A"},
            {"number": 8, "title": "PR", "pull_request": {}},
        ]))
    gh = GitHubClient("t")
    assert [i["number"] for i in await gh.list_ready_issues("o/r")] == [7]


@respx.mock
async def test_issue_dependencies_keep_repo_and_closed_entries():
    respx.get(f"{GH}/repos/o/r/issues/9/dependencies/blocked_by").mock(
        return_value=httpx.Response(200, json=[
            {"number": 3, "state": "open"},
            {"number": 4, "state": "closed",
             "repository": {"full_name": "o/backend"}}]))
    respx.get(f"{GH}/repos/o/r/issues/10/dependencies/blocked_by").mock(
        return_value=httpx.Response(404))
    gh = GitHubClient("t")
    assert await gh.issue_dependencies("o/r", 9) == [
        {"repo": "o/r", "number": 3, "state": "open"},
        {"repo": "o/backend", "number": 4, "state": "closed"}]
    assert await gh.issue_dependencies("o/r", 10) == []


@respx.mock
async def test_issue_blocked_by_is_open_numbers_only():
    respx.get(f"{GH}/repos/o/r/issues/9/dependencies/blocked_by").mock(
        return_value=httpx.Response(200, json=[
            {"number": 3, "state": "open"}, {"number": 4, "state": "closed"}]))
    assert await GitHubClient("t").issue_blocked_by("o/r", 9) == [3]


@respx.mock
async def test_issue_blocking_reverses_the_direction():
    respx.get(f"{GH}/repos/o/r/issues/12/dependencies/blocking").mock(
        return_value=httpx.Response(200, json=[
            {"number": 13, "state": "open",
             "repository": {"full_name": "o/frontend"}}]))
    respx.get(f"{GH}/repos/o/r/issues/14/dependencies/blocking").mock(
        return_value=httpx.Response(410))
    gh = GitHubClient("t")
    assert await gh.issue_blocking("o/r", 12) == [
        {"repo": "o/frontend", "number": 13, "state": "open"}]
    assert await gh.issue_blocking("o/r", 14) == []


@respx.mock
async def test_upsert_marked_comment_edits_the_existing_one():
    respx.get(f"{GH}/repos/o/r/issues/12/comments").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "body": "unrelated"},
            {"id": 2, "body": "<!-- loop:api-contract -->old"}]))
    patch = respx.patch(f"{GH}/repos/o/r/issues/comments/2").mock(
        return_value=httpx.Response(200, json={}))
    await GitHubClient("t").upsert_marked_comment(
        "o/r", 12, "<!-- loop:api-contract -->", "new")
    assert patch.called
    assert json.loads(patch.calls[0].request.content) == {"body": "new"}


@respx.mock
async def test_upsert_marked_comment_creates_when_absent():
    respx.get(f"{GH}/repos/o/r/issues/12/comments").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "body": "unrelated"}]))
    post = respx.post(f"{GH}/repos/o/r/issues/12/comments").mock(
        return_value=httpx.Response(201, json={}))
    await GitHubClient("t").upsert_marked_comment(
        "o/r", 12, "<!-- loop:api-contract -->", "new")
    assert post.called


@respx.mock
async def test_get_repo_default_branch():
    respx.get(f"{GH}/repos/o/r").mock(
        return_value=httpx.Response(200, json={"default_branch": "trunk"}))
    assert await GitHubClient("t").get_repo_default_branch("o/r") == "trunk"


@respx.mock
async def test_list_issue_comments_and_get_issue():
    comments = respx.get(f"{GH}/repos/o/r/issues/9/comments").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "body": "hi"}]))
    respx.get(f"{GH}/repos/o/r/issues/9").mock(
        return_value=httpx.Response(200, json={"number": 9, "state": "open"}))
    gh = GitHubClient("t")
    got = await gh.list_issue_comments("o/r", 9, since="2026-08-03T00:00:00Z")
    assert [c["body"] for c in got] == ["hi"]
    assert comments.calls[0].request.url.params["since"] == "2026-08-03T00:00:00Z"
    assert (await gh.get_issue("o/r", 9))["state"] == "open"
