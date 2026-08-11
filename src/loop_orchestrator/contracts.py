"""Upstream contract protocol: the contracting stage's prompt and verdict, and
the renderings that carry a captured contract to a dependent task.

Mostly pure functions. The two exceptions are `collect_upstreams` and
`fetch_context_files` at the bottom: both the scheduler and the pipeline need
the same resolution order and the same source budget, and two copies of either
would drift. The dependency stays one-way — `db` knows nothing about contracts.
"""
import json
from dataclasses import dataclass, field

from . import db as dbmod
from .jsonextract import find_json_object
from .loopconfig import resolve_base_branch
from .review import WORKING_EFFICIENTLY

COMMENT_MARKER = "<!-- loop:api-contract -->"
COMMENT_END_MARKER = "<!-- /loop:api-contract -->"
CONTEXT_DIR = ".loop/context"
MAX_SOURCES = 10
MAX_CONTEXT_BYTES = 256 * 1024


class ContractError(Exception):
    pass


@dataclass
class Contract:
    outcome: str  # "contract" | "none"
    contract: str = ""
    sources: list[str] = field(default_factory=list)
    breaking_changes: list[str] = field(default_factory=list)


@dataclass
class Upstream:
    """One dependency of a task, with whatever is known about what it built."""
    repo: str
    number: int
    title: str = ""
    pr_number: int | None = None
    contract_md: str = ""
    sources: list[str] = field(default_factory=list)


CONTRACT_OUTPUT_SCHEMA = """{
  "outcome": "contract | none",
  "contract": "markdown: endpoints (method, path, authentication, request schema, response schema, status and error codes), events, shared types",
  "sources": ["repo-relative paths of the files that define the interface"],
  "breaking_changes": ["what existing consumers must change"]
}"""


def parse_contract_output(text: str) -> Contract:
    data = find_json_object(text, "outcome")
    if data is None:
        raise ContractError("no JSON object in the agent message")
    outcome = data.get("outcome")
    if outcome not in ("contract", "none"):
        raise ContractError(f"unknown contract outcome: {outcome!r}")
    body = str(data.get("contract") or "").strip()
    if outcome == "contract" and not body:
        raise ContractError("outcome=contract but the contract body is empty")
    sources = [str(s).strip().lstrip("/")
               for s in (data.get("sources") or []) if str(s).strip()]
    breaking = [str(b).strip()
                for b in (data.get("breaking_changes") or []) if str(b).strip()]
    return Contract(outcome=outcome, contract=body,
                    sources=sources[:MAX_SOURCES], breaking_changes=breaking)


def build_contract_prompt(head_branch: str) -> str:
    return (
        "You have just changed this branch. Tasks in other repositories will be "
        "planned against whatever you describe here — the consumer's planning "
        "agent will never see your code.\n"
        "This is a fresh session: there is no earlier conversation to recall. The "
        "work is exactly what sits on top of the imported branch:\n"
        f"  git diff --stat origin/{head_branch}..HEAD   # which files changed\n"
        f"  git diff origin/{head_branch}..HEAD          # the change itself\n"
        "Uncommitted leftovers count too — `git status --short` shows them.\n\n"
        "Describe the externally consumable interface this branch adds or changes: "
        "HTTP endpoints (method, path, authentication, request schema, response "
        "schema, status and error codes), events, and shared types. Verify every "
        "item against the source — anything the code does not implement must not "
        "appear in the description. List separately any breaking change to an "
        "interface that already had consumers.\n"
        f"List the authoritative source files a reader should open to check you: at "
        f"most {MAX_SOURCES} repo-relative paths, the ones that define the interface "
        "rather than the ones that merely use it.\n"
        'If the branch exposes no externally consumable interface at all, answer with '
        'outcome "none" — that is a real answer, not a failure.\n'
        "Do NOT modify, commit or push anything — you only describe.\n\n"
        + WORKING_EFFICIENTLY +
        "\nYour FINAL message must be a single JSON object and nothing else, "
        "matching exactly this schema:\n"
        f"{CONTRACT_OUTPUT_SCHEMA}"
    )


def render_contract_comment(contract: Contract, pr_number: int,
                            head_sha: str) -> str:
    """The issue comment. The body between the markers is the contract itself,
    so a human edit of it can be read back verbatim."""
    body = (contract.contract if contract.outcome == "contract"
            else "This change exposes no externally consumable interface.")
    lines = [COMMENT_MARKER,
             "**🤖 loop-orchestrator — API contract for dependent tasks**", "",
             body, COMMENT_END_MARKER, ""]
    if contract.breaking_changes:
        lines += ["**Breaking changes:**"]
        lines += [f"- {b}" for b in contract.breaking_changes]
        lines += [""]
    if contract.sources:
        lines += ["**Authoritative sources:**"]
        lines += [f"- `{s}`" for s in contract.sources]
        lines += [""]
    lines += [f"Captured from PR #{pr_number} at `{head_sha[:7]}`. Edit the text "
              "above this line to correct it — the planning agent of every "
              "dependent task reads this comment in preference to the stored copy."]
    return "\n".join(lines)


def extract_contract(comment_body: str) -> str:
    """The contract text a human may have edited, from a marked comment."""
    body = comment_body or ""
    if COMMENT_MARKER in body and COMMENT_END_MARKER in body:
        head = body.split(COMMENT_MARKER, 1)[1]
        inner = head.split(COMMENT_END_MARKER, 1)[0]
        # The bot heading is the first line of the block; a hand-written
        # replacement has no heading and must survive untouched.
        lines = [ln for ln in inner.strip().splitlines()
                 if not ln.startswith("**🤖 loop-orchestrator")]
        return "\n".join(lines).strip()
    return body.replace(COMMENT_MARKER, "").replace(COMMENT_END_MARKER, "").strip()


def render_upstream_section(upstreams: list[Upstream]) -> str:
    """The `.loop/task.md` block. Rendered whenever dependencies exist, with or
    without a contract: silence reads as "there was no upstream"."""
    if not upstreams:
        return ""
    out = ["## Upstream dependencies", ""]
    for u in upstreams:
        head = f"### {u.repo}#{u.number}"
        if u.title:
            head += f" — {u.title}"
        if u.pr_number:
            head += f" (PR #{u.pr_number})"
        out += [head, ""]
        if u.contract_md:
            out += ["**Upstream API contract — authoritative, do not invent "
                    "endpoints.**", "", u.contract_md, ""]
        else:
            out += ["No contract digest was captured for this dependency. Do not "
                    "guess its interface: read its code if it is reachable, "
                    "otherwise ask.", ""]
        if u.sources:
            out += [f"Authoritative sources, fetched into "
                    f"`{CONTEXT_DIR}/{u.repo}/`:"]
            out += [f"- `{s}`" for s in u.sources]
            out += [""]
    return "\n".join(out)


def render_context_readme(upstreams: list[Upstream], dropped: list[str]) -> str:
    lines = ["# Upstream context", "",
             "Read-only copies of the files that define the interfaces this task "
             "consumes. They are the authority: where this directory and any "
             "description disagree, these files are right.", ""]
    for u in upstreams:
        lines += [f"- `{u.repo}#{u.number}` → `{CONTEXT_DIR}/{u.repo}/`"]
    if dropped:
        lines += ["", f"Dropped to stay within {MAX_CONTEXT_BYTES // 1024} KiB:"]
        lines += [f"- {d}" for d in dropped]
    return "\n".join(lines) + "\n"


def missing_source_note(repo: str, base: str, path: str) -> str:
    return (f"This file was named as an authoritative source of {repo}, but "
            f"`{path}` does not exist on `{base}` — it was renamed or removed "
            "after the contract was captured. Do not assume its contents.\n")


async def collect_upstreams(db, gh, task) -> list[Upstream]:
    """What every dependency of `task` built, best available source first.

    Contract text: the marked issue comment (a human may have corrected it),
    then the stored row. Everything else — sources, PR number, title — comes
    from the row and the API. A dependency with nothing recorded still yields an
    Upstream: the section it renders is what stops the planner from guessing.
    """
    out: list[Upstream] = []
    for dep in task.depends_on:
        repo, number = dep["repo"], dep["number"]
        row = await dbmod.get_contract(db, repo, number)
        contract_md = row["contract_md"] if row else ""
        sources = json.loads(row["sources_json"]) if row else []
        pr_number = row["pr_number"] if row else None
        try:
            comment = await gh.find_comment(repo, number, COMMENT_MARKER)
        except Exception:  # noqa: BLE001 — the stored row is the fallback
            comment = None
        if comment is not None:
            contract_md = extract_contract(comment.get("body") or "")
        if pr_number is None:
            run = await dbmod.latest_run_for_issue(db, repo, number, "pr")
            pr_number = run.pr_number if run else None
        title = ""
        try:
            title = (await gh.get_issue(repo, number)).get("title") or ""
        except Exception:  # noqa: BLE001 — a heading, not a requirement
            pass
        out.append(Upstream(repo=repo, number=number, title=title,
                            pr_number=pr_number, contract_md=contract_md,
                            sources=sources))
    return out


async def fetch_context_files(gh, upstreams: list[Upstream]
                              ) -> tuple[dict[str, str], list[str]]:
    """Read every authoritative source off its producer's base branch.

    The base branch and not a pinned sha: the consumer is planned against what
    is in the trunk when it starts. A source that no longer exists becomes a
    note rather than a silent absence — a promised file that vanishes without
    trace is how a planner talks itself into guessing.
    """
    out: dict[str, str] = {}
    dropped: list[str] = []
    total = 0
    for u in upstreams:
        if not u.sources:
            continue
        base = await resolve_base_branch(gh, u.repo)
        for src in u.sources[:MAX_SOURCES]:
            path = f"{CONTEXT_DIR}/{u.repo}/{src}"
            text = await gh.get_file(u.repo, base, src)
            if text is None:
                out[path] = missing_source_note(u.repo, base, src)
                continue
            size = len(text.encode("utf-8", "replace"))
            if total + size > MAX_CONTEXT_BYTES:
                dropped.append(f"{u.repo}/{src}")
                continue
            total += size
            out[path] = text
    return out, dropped
