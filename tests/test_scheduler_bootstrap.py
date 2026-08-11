from tests.conftest import FakeGitHub

from loop_orchestrator.contracts import Upstream
from loop_orchestrator.scheduler import bootstrap, branch_for_issue, lane_from_labels


def test_branch_and_lane_helpers():
    assert branch_for_issue(7) == "loop/issue-7"
    assert lane_from_labels([{"name": "loop:ready"}, {"name": "loop:lane:auth"}]) == "auth"
    assert lane_from_labels([{"name": "loop:ready"}]) is None


async def test_bootstrap_creates_branch_and_task_file():
    gh = FakeGitHub()
    gh.branch_shas["main"] = "basesha"
    branch = await bootstrap(gh, "o/r", {"number": 7, "title": "T", "body": "B",
                                         "labels": []}, [])
    assert branch == "loop/issue-7"
    assert gh.branches_created == [("loop/issue-7", "basesha")]
    assert gh.files_put[0][0:2] == ("loop/issue-7", ".loop/task.md")
    assert "# Issue #7" in gh.files_put[0][2]


async def test_bootstrap_is_idempotent_for_existing_branch():
    gh = FakeGitHub()
    gh.branch_shas["main"] = "basesha"
    gh.branch_shas["loop/issue-7"] = "existing"
    await bootstrap(gh, "o/r", {"number": 7, "title": "T", "body": "B",
                                "labels": []}, [])
    assert gh.branches_created == []          # branch untouched
    assert len(gh.files_put) == 1             # task file refreshed


async def test_bootstrap_forks_from_the_configured_base_branch():
    # A repo whose deploy branch is not the trunk points loop work at it, so
    # the issue branch must fork from `staging` and not from the default.
    gh = FakeGitHub()
    gh.branch_shas["main"] = "mainsha"
    gh.branch_shas["staging"] = "stagingsha"
    gh.files[".loop.yml"] = "specs_dir: docs/specs\nbase_branch: staging\n"
    await bootstrap(gh, "o/r", {"number": 7, "title": "T", "body": "B",
                                "labels": []}, [])
    assert gh.branches_created == [("loop/issue-7", "stagingsha")]


async def test_bootstrap_commits_the_upstream_section():
    gh = FakeGitHub()
    gh.branch_shas["main"] = "basesha"
    await bootstrap(gh, "o/frontend",
                    {"number": 13, "title": "F", "body": "B", "labels": []}, [],
                    [Upstream(repo="o/backend", number=12,
                              contract_md="### POST /v1/x")])
    assert "## Upstream dependencies" in gh.files_put[0][2]
