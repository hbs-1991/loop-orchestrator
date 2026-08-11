from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml


class LoopConfigError(Exception):
    pass


@dataclass
class LoopConfig:
    specs_dir: str
    # Branch new work forks from and PRs land on. None = the repository's
    # default branch. Set it when the trunk is not where work should land —
    # e.g. a repo that auto-deploys from `staging`.
    base_branch: str | None = None
    setup: str | None = None
    run: str | None = None
    test: str | None = None
    required_env: list[str] = field(default_factory=list)
    timeout_minutes: int | None = None
    sandbox_preset: str | None = None
    review_enabled: bool = True
    review_max_fix_iterations: int | None = None
    e2e_enabled: bool = False
    e2e_max_fix_iterations: int | None = None
    e2e_env: dict[str, str] = field(default_factory=dict)
    e2e_services: bool = False
    approval: str = "always"  # always | never — pause before publishing
    # Planning is the one stage a repository may switch off entirely: it is
    # opt-out rather than opt-in, because a backlog issue with no plan has
    # nowhere else to get one. `None` on a model means "whatever LOOP_* says".
    planning_enabled: bool = True
    planner_model: str | None = None
    advisor_enabled: bool = True
    advisor_model: str | None = None
    plan_max_iterations: int | None = None


def parse_loop_config(text: str) -> LoopConfig:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise LoopConfigError(f"invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise LoopConfigError("expected a YAML mapping")
    specs_dir = data.get("specs_dir")
    if not isinstance(specs_dir, str) or not specs_dir.strip():
        raise LoopConfigError("specs_dir is required")
    required_env = data.get("required_env", [])
    if not (isinstance(required_env, list) and all(isinstance(x, str) for x in required_env)):
        raise LoopConfigError("required_env must be a list of strings")
    timeout = data.get("timeout_minutes")
    if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
        raise LoopConfigError("timeout_minutes must be a positive integer")

    def opt_str(key: str) -> str | None:
        v = data.get(key)
        if v is not None and not isinstance(v, str):
            raise LoopConfigError(f"{key} must be a string")
        return v

    review = data.get("review") or {}
    if not isinstance(review, dict):
        raise LoopConfigError("review must be a mapping")
    review_enabled = review.get("enabled", True)
    if not isinstance(review_enabled, bool):
        raise LoopConfigError("review.enabled must be a boolean")
    max_fix = review.get("max_fix_iterations")
    if max_fix is not None and (not isinstance(max_fix, int)
                                or isinstance(max_fix, bool) or max_fix < 0):
        raise LoopConfigError("review.max_fix_iterations must be an integer >= 0")

    e2e_raw = data.get("e2e")
    if e2e_raw is not None and not isinstance(e2e_raw, dict):
        raise LoopConfigError("e2e must be a mapping")
    e2e = e2e_raw or {}
    e2e_enabled = e2e.get("enabled", True)
    if not isinstance(e2e_enabled, bool):
        raise LoopConfigError("e2e.enabled must be a boolean")
    e2e_max = e2e.get("max_fix_iterations")
    if e2e_max is not None and (not isinstance(e2e_max, int)
                                or isinstance(e2e_max, bool) or e2e_max < 0):
        raise LoopConfigError("e2e.max_fix_iterations must be an integer >= 0")
    e2e_env = e2e.get("env") or {}
    if not (isinstance(e2e_env, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in e2e_env.items())):
        raise LoopConfigError("e2e.env must be a mapping of string to string")

    approval = data.get("approval", "always")
    if approval not in ("always", "never"):
        raise LoopConfigError("approval must be 'always' or 'never'")

    planning = data.get("planning")
    if planning is not None and not isinstance(planning, dict):
        raise LoopConfigError("planning must be a mapping")
    planning = planning or {}
    advisor = planning.get("advisor")
    if advisor is not None and not isinstance(advisor, dict):
        raise LoopConfigError("planning.advisor must be a mapping")
    advisor = advisor or {}

    def flag(section: dict, path: str, key: str = "enabled") -> bool:
        v = section.get(key, True)
        if not isinstance(v, bool):
            raise LoopConfigError(f"{path} must be a boolean")
        return v

    def model(section: dict, path: str) -> str | None:
        v = section.get("model")
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise LoopConfigError(f"{path} must be a non-empty string")
        return v.strip()

    plan_iterations = advisor.get("max_iterations")
    if plan_iterations is not None and (not isinstance(plan_iterations, int)
                                        or isinstance(plan_iterations, bool)
                                        or plan_iterations < 0):
        raise LoopConfigError(
            "planning.advisor.max_iterations must be an integer >= 0")

    base_branch = opt_str("base_branch")
    if base_branch is not None and not base_branch.strip():
        raise LoopConfigError("base_branch must not be empty")

    return LoopConfig(
        specs_dir=specs_dir.strip().strip("/"),
        base_branch=base_branch.strip() if base_branch else None,
        setup=opt_str("setup"),
        run=opt_str("run"),
        test=opt_str("test"),
        required_env=required_env,
        timeout_minutes=timeout,
        sandbox_preset=opt_str("sandbox_preset"),
        review_enabled=review_enabled,
        review_max_fix_iterations=max_fix,
        e2e_enabled=(e2e_raw is not None) and e2e_enabled,
        e2e_max_fix_iterations=e2e_max,
        e2e_env=e2e_env,
        e2e_services=e2e.get("services") is not None,
        approval=approval,
        planning_enabled=flag(planning, "planning.enabled"),
        planner_model=model(planning, "planning.model"),
        advisor_enabled=flag(advisor, "planning.advisor.enabled"),
        advisor_model=model(advisor, "planning.advisor.model"),
        plan_max_iterations=plan_iterations,
    )


async def resolve_base_branch(gh, repo: str) -> str:
    """The branch loop work forks from and its PRs land on.

    The repository's default branch unless `.loop.yml` overrides it with
    `base_branch`. Read from the DEFAULT branch on purpose: the override cannot
    describe where to find itself, and reading it from the base would need the
    answer first.

    Never fatal — a repo with no config, or one whose config is broken, simply
    keeps the default branch. The run then fails later, on `preparing`, with the
    real parse error instead of an obscure failure while creating a branch.
    """
    default = await gh.get_repo_default_branch(repo)
    raw = await gh.get_file(repo, default, ".loop.yml")
    if raw is None:
        return default
    try:
        return parse_loop_config(raw).base_branch or default
    except LoopConfigError:
        return default


async def planning_enabled(gh, repo: str) -> bool:
    """Whether the scheduler may open a planning Run for this repository.

    Read from the DEFAULT branch, like `resolve_base_branch` and for the same
    reason: the decision is taken before the issue branch exists, so there is
    nowhere else to read it from.

    Fail-safe **on**: no config, an unreadable one or a broken one means the
    repository is planned as it always was. A repository that means to switch
    planning off says so in a `.loop.yml` that parses; anything else is a
    fetch that failed, and a failed fetch must not silently stop a backlog.
    """
    try:
        default = await gh.get_repo_default_branch(repo)
        raw = await gh.get_file(repo, default, ".loop.yml")
    except Exception:  # noqa: BLE001 — GitHub blip, not a policy statement
        return True
    if raw is None:
        return True
    try:
        return parse_loop_config(raw).planning_enabled
    except LoopConfigError:
        return True


def plans_dir(specs_dir: str) -> str:
    return str(PurePosixPath(specs_dir).parent / "plans")


def find_spec_plan_pair(files: list[str], cfg: LoopConfig) -> tuple[str, str]:
    specs = [f for f in files if f.startswith(cfg.specs_dir + "/") and f.endswith("-design.md")]
    pdir = plans_dir(cfg.specs_dir)
    plans = [f for f in files if f.startswith(pdir + "/") and f.endswith(".md")]
    if len(specs) != 1:
        raise LoopConfigError(
            f"the PR diff must contain exactly one *-design.md spec under {cfg.specs_dir}/ (found {len(specs)})")
    if len(plans) != 1:
        raise LoopConfigError(
            f"the PR diff must contain exactly one *.md plan under {pdir}/ (found {len(plans)})")
    return specs[0], plans[0]
