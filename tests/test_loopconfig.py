import pytest

from loop_orchestrator.loopconfig import (
    LoopConfigError, find_spec_plan_pair, parse_loop_config, plans_dir,
    resolve_base_branch,
)

FULL = """
specs_dir: docs/superpowers/specs
setup: npm install
test: npm test
required_env: [DATABASE_URL, API_KEY]
timeout_minutes: 60
sandbox_preset: node
e2e:
  services: []
"""


def test_parse_full():
    cfg = parse_loop_config(FULL)
    assert cfg.specs_dir == "docs/superpowers/specs"
    assert cfg.required_env == ["DATABASE_URL", "API_KEY"]
    assert cfg.timeout_minutes == 60
    assert cfg.sandbox_preset == "node"
    assert cfg.run is None  # e2e is ignored, run is not set


def test_specs_dir_required():
    with pytest.raises(LoopConfigError):
        parse_loop_config("setup: npm install")
    with pytest.raises(LoopConfigError):
        parse_loop_config("- just\n- a list")


def test_bad_timeout_and_required_env():
    with pytest.raises(LoopConfigError):
        parse_loop_config("specs_dir: d\ntimeout_minutes: -5")
    with pytest.raises(LoopConfigError):
        parse_loop_config("specs_dir: d\nrequired_env: notalist")


def test_plans_dir():
    assert plans_dir("docs/superpowers/specs") == "docs/superpowers/plans"


def test_find_pair():
    cfg = parse_loop_config("specs_dir: docs/superpowers/specs")
    files = [
        "src/a.py",
        "docs/superpowers/specs/2026-07-31-x-design.md",
        "docs/superpowers/plans/2026-07-31-x.md",
    ]
    assert find_spec_plan_pair(files, cfg) == (files[1], files[2])


def test_review_defaults_without_block():
    cfg = parse_loop_config("specs_dir: docs/specs\n")
    assert cfg.review_enabled is True
    assert cfg.review_max_fix_iterations is None


def test_review_block_parsed():
    cfg = parse_loop_config(
        "specs_dir: docs/specs\nreview:\n  enabled: false\n  max_fix_iterations: 0\n")
    assert cfg.review_enabled is False
    assert cfg.review_max_fix_iterations == 0


def test_review_block_invalid_types():
    with pytest.raises(LoopConfigError):
        parse_loop_config("specs_dir: d\nreview: nope\n")
    with pytest.raises(LoopConfigError):
        parse_loop_config("specs_dir: d\nreview:\n  enabled: 5\n")
    with pytest.raises(LoopConfigError):
        parse_loop_config("specs_dir: d\nreview:\n  max_fix_iterations: -1\n")


def test_e2e_absent_disabled():
    cfg = parse_loop_config("specs_dir: docs/specs\n")
    assert cfg.e2e_enabled is False
    assert cfg.e2e_env == {}
    assert cfg.e2e_services is False


def test_e2e_block_enables():
    cfg = parse_loop_config(
        "specs_dir: docs/specs\nrun: npm run dev\n"
        "e2e:\n  env:\n    VITE_API_URL: http://localhost:8000\n")
    assert cfg.e2e_enabled is True
    assert cfg.e2e_env == {"VITE_API_URL": "http://localhost:8000"}
    assert cfg.e2e_max_fix_iterations is None


def test_e2e_explicit_disable():
    cfg = parse_loop_config("specs_dir: docs/specs\ne2e:\n  enabled: false\n")
    assert cfg.e2e_enabled is False


def test_e2e_max_fix_iterations():
    cfg = parse_loop_config("specs_dir: docs/specs\ne2e:\n  max_fix_iterations: 0\n")
    assert cfg.e2e_max_fix_iterations == 0


def test_e2e_bad_max_fix_iterations():
    with pytest.raises(LoopConfigError):
        parse_loop_config("specs_dir: docs/specs\ne2e:\n  max_fix_iterations: -1\n")


def test_e2e_bad_enabled():
    with pytest.raises(LoopConfigError):
        parse_loop_config("specs_dir: docs/specs\ne2e:\n  enabled: yes please\n")


def test_e2e_env_must_be_string_map():
    with pytest.raises(LoopConfigError):
        parse_loop_config("specs_dir: docs/specs\ne2e:\n  env:\n    PORT: 8000\n")


def test_e2e_services_flagged():
    cfg = parse_loop_config(
        "specs_dir: docs/specs\ne2e:\n  services:\n    - repo: o/backend\n")
    assert cfg.e2e_services is True


def test_e2e_not_a_mapping():
    with pytest.raises(LoopConfigError):
        parse_loop_config("specs_dir: docs/specs\ne2e: true\n")


def test_approval_default_always():
    cfg = parse_loop_config("specs_dir: docs/specs")
    assert cfg.approval == "always"


def test_approval_never():
    cfg = parse_loop_config("specs_dir: docs/specs\napproval: never")
    assert cfg.approval == "never"


def test_approval_invalid_rejected():
    with pytest.raises(LoopConfigError, match="approval"):
        parse_loop_config("specs_dir: docs/specs\napproval: sometimes")


def test_find_pair_errors():
    cfg = parse_loop_config("specs_dir: docs/superpowers/specs")
    with pytest.raises(LoopConfigError):
        find_spec_plan_pair(["src/a.py"], cfg)
    with pytest.raises(LoopConfigError):
        find_spec_plan_pair(
            ["docs/superpowers/specs/a-design.md", "docs/superpowers/specs/b-design.md",
             "docs/superpowers/plans/a.md"],
            cfg,
        )


def test_base_branch_defaults_to_none():
    assert parse_loop_config("specs_dir: docs/specs").base_branch is None


def test_base_branch_parsed_and_trimmed():
    cfg = parse_loop_config("specs_dir: docs/specs\nbase_branch: '  staging  '")
    assert cfg.base_branch == "staging"


def test_base_branch_rejects_blank():
    with pytest.raises(LoopConfigError, match="base_branch"):
        parse_loop_config("specs_dir: docs/specs\nbase_branch: '   '")


class _GH:
    def __init__(self, default="main", file=None):
        self.default, self.file = default, file

    async def get_repo_default_branch(self, repo):
        return self.default

    async def get_file(self, repo, ref, path):
        self.read_from = ref
        return self.file


async def test_resolve_base_branch_uses_the_default_without_config():
    assert await resolve_base_branch(_GH(), "o/r") == "main"


async def test_resolve_base_branch_honours_the_override():
    gh = _GH(file="specs_dir: docs/specs\nbase_branch: staging")
    assert await resolve_base_branch(gh, "o/r") == "staging"
    # Read from the default branch: the override cannot say where to find itself.
    assert gh.read_from == "main"


async def test_resolve_base_branch_survives_a_broken_config():
    # The run still fails later, on preparing, with the real parse error —
    # better than an obscure failure while creating the issue branch.
    gh = _GH(file="specs_dir:\n  - not a string")
    assert await resolve_base_branch(gh, "o/r") == "main"
