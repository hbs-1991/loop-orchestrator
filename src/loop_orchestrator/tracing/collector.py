"""Getting the session file out of a sandbox.

Two facts decide the shape of this, both verified against the sandboxd sources:

- The files API is rooted at `<mount>/workspace/app` (`appSubdir` in
  `internal/api/v1_files.go`) and refuses to follow a symlink out of it
  (`realpathWithin`, CWE-59). Claude Code's sessions live in HOME —
  `/home/sandbox/.claude/projects/<slug>/<uuid>.jsonl` — so `list_files` and
  `read_file` cannot see them at all.
- `exec_cmd` runs as the image's user (`sandbox`, uid 1000), which owns that
  directory.

So exec copies the file into the app directory and the files API carries it out.
The alternative — cat it through exec's stdout — puts megabytes through a JSON
string field on an endpoint meant for short commands.
"""
import logging

log = logging.getLogger(__name__)

# Under `.loop/`, which already holds secrets.env next to a `.gitignore`
# containing `*`. The copy is therefore invisible to git and cannot ride a commit
# out of the sandbox even if the agent runs `git add -A`.
TRACE_DIR = ".loop/trace"


def copy_script(dest: str) -> str:
    """Newest session file -> `dest`, relative to the app directory.

    `exec_cmd` passes neither `-u` nor `-w` (sandboxd's `Client.Exec`), so the
    command lands in the image's WORKDIR and has to find the app itself. `ls -t`
    rather than `find -newer`: the sessions of every stage of this run sit in the
    same directory, and the newest is the one that just finished.
    """
    return (
        'set -e; '
        'app="$HOME/workspace/app"; [ -d "$app" ] || app="$PWD"; '
        'src=$(ls -t "$HOME"/.claude/projects/*/*.jsonl 2>/dev/null | head -n 1); '
        '[ -n "$src" ] || { echo no-session >&2; exit 3; }; '
        f'mkdir -p "$app/{TRACE_DIR}"; '
        f'cp "$src" "$app/{dest}"; '
        f'wc -c < "$app/{dest}"'
    )


async def fetch_session(sb, sandbox_id: str, stage: str) -> bytes | None:
    """The raw JSONL of the session that just ran, or None.

    Best-effort at every step: a dead sandbox, a missing session or a files API
    that refuses the path all mean "no trace for this stage", never an error the
    caller has to handle.
    """
    if not sandbox_id:
        return None
    dest = f"{TRACE_DIR}/{stage}.jsonl"
    try:
        res = await sb.exec_cmd(sandbox_id, ["sh", "-c", copy_script(dest)])
    except Exception:  # noqa: BLE001
        log.debug("trace: exec failed for stage %s", stage, exc_info=True)
        return None
    if (res or {}).get("exit_code") != 0:
        log.debug("trace: no session for stage %s (%s)", stage,
                  (res or {}).get("stderr", "")[:200])
        return None
    try:
        raw = await sb.read_file(sandbox_id, dest)
    except Exception:  # noqa: BLE001
        log.debug("trace: read_file failed for stage %s", stage, exc_info=True)
        return None
    return raw or None
