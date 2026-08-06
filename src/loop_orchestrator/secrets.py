import shlex
from pathlib import Path

# Where a run's secrets land inside the sandbox. Under `.loop/` because that
# directory is already the agents' scratch area and is gitignored, so a secret
# can never ride a commit out of the sandbox.
SECRETS_FILE = ".loop/secrets.env"

SOURCE_LINE = f"set -a; . {SECRETS_FILE}; set +a"

# Written next to the secrets. `.loop/` is NOT gitignored in every repo — the
# backend one has `.loop/task.md` committed — and an agent that runs
# `git add -A` would put the credentials in a commit. A `*` gitignore inside
# the directory covers itself and every sibling regardless of what the
# repository's own .gitignore says. Files git already tracks stay tracked, so
# nothing else about a run changes.
SECRETS_GITIGNORE = ".loop/.gitignore"


def render_env_file(secrets: dict[str, str]) -> str:
    """A `set -a`-sourceable env file. Values are shell-quoted, so passwords
    with spaces, quotes or `$` survive intact."""
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in secrets.items())


def source_hint(secrets: dict[str, str]) -> str:
    """Prompt fragment telling the agent how to load the run's secrets.

    Names only — a value in a prompt would be persisted in the run record and
    in sandboxd's task log.
    """
    if not secrets:
        return ""
    names = ", ".join(f"`{k}`" for k in secrets)
    return (
        f"Secrets for this project ({names}) are in `{SECRETS_FILE}`. Load them "
        f"with `{SOURCE_LINE}` in every shell that needs them, before starting "
        "the app or running tests. Never print their values, and never commit "
        "the file.\n"
    )


def load_repo_secrets(secrets_dir: str, repo: str) -> dict[str, str]:
    path = Path(secrets_dir) / (repo.replace("/", "__") + ".env")
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out
