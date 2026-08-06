"""Markdown -> Telegram HTML converter for agent summaries.

Telegram bots style text with message entities, expressed in the Bot API as a
small HTML subset (parse_mode=HTML): b/i/s/u, code/pre, a, blockquote
(optionally expandable). Agent summaries arrive as Markdown, so we map the
common constructs onto that subset and escape everything else.
"""
import html
import re

_FENCE = re.compile(r"```[^\S\n]*(\w*)\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_STAR = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_STRIKE = re.compile(r"~~(.+?)~~")
_HEADER = re.compile(r"^#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*]\s+(.*)$")
_STASH = re.compile(r"\x00(\d+)\x00")
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_CELL = re.compile(r"^:?-+:?$")
_CELL_CODE = re.compile(r"`([^`]*)`")
_CELL_LINK = re.compile(r"\[([^\]]+)\]\([^)\s]+\)")


def _render_table(block: list[str]) -> str:
    """Render markdown table lines as a column-aligned <pre> block.

    Telegram has no table entities in any parse mode, so monospace
    alignment is the only way a table survives rendering.
    """
    rows: list[list[str]] = []
    has_header = False
    for i, line in enumerate(block):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and all(_TABLE_SEP_CELL.match(c) for c in cells):
            has_header = has_header or i == 1
            continue
        rows.append([
            _BOLD.sub(r"\1", _CELL_LINK.sub(r"\1", _CELL_CODE.sub(r"\1", c)))
            for c in cells
        ])
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    widths = [max((len(r[i]) for r in rows if i < len(r)), default=0)
              for i in range(ncols)]
    lines = ["  ".join((r[i] if i < len(r) else "").ljust(widths[i])
                       for i in range(ncols)).rstrip()
             for r in rows]
    if has_header and len(lines) > 1:
        lines.insert(1, "-" * (sum(widths) + 2 * (ncols - 1)))
    return f"<pre>{html.escape(chr(10).join(lines))}</pre>"


def md_to_telegram_html(text: str) -> str:
    """Convert Markdown to the HTML subset Telegram's parse_mode=HTML accepts."""
    stash: list[str] = []

    def _put(rendered: str) -> str:
        stash.append(rendered)
        return f"\x00{len(stash) - 1}\x00"

    # Code first: its content must survive verbatim (escaped, not styled).
    text = _FENCE.sub(lambda m: _put(f"<pre>{html.escape(m.group(2).rstrip())}</pre>"), text)

    # Tables next: rendered whole into <pre>, before inline styling touches cells.
    new_lines: list[str] = []
    buf: list[str] = []

    def _flush() -> None:
        if len(buf) >= 2:
            new_lines.append(_put(_render_table(buf)))
        else:
            new_lines.extend(buf)
        buf.clear()

    for line in text.splitlines():
        if _TABLE_LINE.match(line):
            buf.append(line)
        else:
            _flush()
            new_lines.append(line)
    _flush()
    text = "\n".join(new_lines)

    text = _INLINE_CODE.sub(lambda m: _put(f"<code>{html.escape(m.group(1))}</code>"), text)

    text = html.escape(text)

    text = _LINK.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC_STAR.sub(r"<i>\1</i>", text)
    text = _ITALIC_UNDER.sub(r"<i>\1</i>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)

    lines = []
    for line in text.splitlines():
        if m := _HEADER.match(line):
            lines.append(f"<b>{m.group(1)}</b>")
        elif m := _BULLET.match(line):
            lines.append(f"{m.group(1)}• {m.group(2)}")
        elif line.startswith("&gt; "):
            lines.append(line[5:])  # md quote marker; real quoting is done by caller
        else:
            lines.append(line)
    text = "\n".join(lines)

    return _STASH.sub(lambda m: stash[int(m.group(1))], text)
