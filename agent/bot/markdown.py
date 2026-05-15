"""Telegram MarkdownV2 rendering + message chunking.

Pure helpers — no Bot state. Imported by telegram_bot.py and re-exported as
module-level names so existing callers stay unchanged.

Telegram's MarkdownV2 escape rules are strict and unforgiving
(https://core.telegram.org/bots/api#markdownv2-style); every char in
`_MDV2_SPECIALS` must be backslash-escaped outside an entity. Inside code
spans/blocks only ``` ` ``` and `\\` are special.
"""
from __future__ import annotations

import re


# Every char in this set must be backslash-escaped outside an entity.
_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_MDV2_ESCAPE = {c: "\\" + c for c in _MDV2_SPECIALS}

_STEP_SEPARATOR = "\n---------------\n"


def _escape_mdv2_plain(s: str) -> str:
    """Backslash-escape every MarkdownV2 special char in plain text."""
    return "".join(_MDV2_ESCAPE.get(c, c) for c in s)


def _escape_mdv2_code(s: str) -> str:
    """Inside code spans / blocks, only ` and \\ need escaping."""
    return s.replace("\\", "\\\\").replace("`", "\\`")


def _to_tg_markdown_v2(text: str) -> str:
    """Convert claude's standard markdown to Telegram MarkdownV2.

    Handles the formatting claude actually emits: fenced code blocks,
    inline code, **bold** / __bold__, *italic* / _italic_, [link](url).
    Anything else is plain text and gets the full escape pass. The
    400-fallback in send() covers gaps in this converter.
    """
    # 1) Pull fenced code blocks first so their bodies skip the inline pass.
    blocks: list[str] = []

    def _stash_block(m):
        lang = (m.group(1) or "").strip()
        body = _escape_mdv2_code(m.group(2))
        blocks.append(f"```{lang}\n{body}\n```")
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    text = re.sub(r"```([^\n`]*)\n(.*?)```", _stash_block, text, flags=re.DOTALL)

    # 2) Inline code spans.
    codes: list[str] = []

    def _stash_code(m):
        codes.append("`" + _escape_mdv2_code(m.group(1)) + "`")
        return f"\x00CODE{len(codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _stash_code, text)

    # 3) Bold / italic / links — interleave with plain text that gets full escape.
    pattern = re.compile(
        r"\*\*(.+?)\*\*"  # **bold**
        r"|__(.+?)__"  # __bold__
        r"|(?<![*\w])\*([^*\n]+?)\*(?!\w)"  # *italic*
        r"|(?<![_\w])_([^_\n]+?)_(?!\w)"  # _italic_
        r"|\[([^\]\n]+)\]\(([^)\n]+)\)"  # [text](url)
    )

    def _render(m):
        bold = m.group(1) or m.group(2)
        italic = m.group(3) or m.group(4)
        link_text = m.group(5)
        link_url = m.group(6)
        if bold is not None:
            return "*" + _escape_mdv2_plain(bold) + "*"
        if italic is not None:
            return "_" + _escape_mdv2_plain(italic) + "_"
        url = link_url.replace("\\", "\\\\").replace(")", "\\)")
        return "[" + _escape_mdv2_plain(link_text) + "](" + url + ")"

    out: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            out.append(_escape_mdv2_plain(text[pos : m.start()]))
        out.append(_render(m))
        pos = m.end()
    if pos < len(text):
        out.append(_escape_mdv2_plain(text[pos:]))
    rendered = "".join(out)

    # 4) Restore stashed code (already escaped inside).
    rendered = re.sub(r"\x00CODE(\d+)\x00", lambda m: codes[int(m.group(1))], rendered)
    rendered = re.sub(r"\x00BLOCK(\d+)\x00", lambda m: blocks[int(m.group(1))], rendered)
    return rendered


def _render_expandable_blockquote(text: str) -> str:
    """Wrap `text` in a Telegram MarkdownV2 expandable blockquote.

    Syntax: first line starts with `**>`, subsequent lines with `>`, and
    the whole thing closes with `||` appended to the last line. The body
    is escaped as plain MDV2 — we don't try to honor inline markdown
    inside the collapsed section.

    Returns "" for empty input so callers can build conditional sections.
    """
    if not text or not text.strip():
        return ""
    lines = text.split("\n")
    escaped = [_escape_mdv2_plain(line) for line in lines]
    out: list[str] = []
    for i, line in enumerate(escaped):
        prefix = "**>" if i == 0 else ">"
        out.append(prefix + line)
    out[-1] = out[-1] + "||"
    return "\n".join(out)


def _build_header(total: int, shown: int, sub_agents: int, marker: str) -> str:
    """Compose the first (collapsed-visible) line of the blockquote.

    `<marker> N messages` always. When trimming kicks in we extend with
    `(last K shown)` so the count remains honest. When sub-agents have
    been spawned we append ` · 🤖 +M sub-agents`.
    """
    if shown < total:
        head = f"{marker} {total} messages (last {shown} shown)"
    else:
        head = f"{marker} {total} message" + ("s" if total != 1 else "")
    if sub_agents > 0:
        head += f" · 🤖 +{sub_agents} sub-agent" + ("s" if sub_agents != 1 else "")
    return head


def _render_collapsed_steps(
    parts: list[str],
    total: int,
    max_body: int,
    sub_agents: int = 0,
    trailer: str = "",
    marker: str = "💭",
) -> str:
    """Render `parts` as one expandable blockquote with a message-count header.

    Trims OLDEST blocks until rendered body fits under `max_body`; keeps at
    least the most recent one so something always renders.
    """
    if not parts:
        return ""
    work = list(parts)
    while True:
        body = _build_header(total, len(work), sub_agents, marker) + "\n" + _STEP_SEPARATOR.join(work)
        if trailer:
            body += _STEP_SEPARATOR + trailer
        out = _render_expandable_blockquote(body)
        if len(out) <= max_body or len(work) <= 1:
            return out
        work = work[1:]


def _render_streaming_view(
    blocks: list[str],
    max_body: int,
    sub_agents: int = 0,
    marker: str = "💭",
) -> str:
    """Render every assistant text block as one collapsed blockquote."""
    parts = [b.strip() for b in blocks if b and b.strip()]
    if not parts:
        return ""
    return _render_collapsed_steps(parts, len(parts), max_body, sub_agents=sub_agents, marker=marker)


def _fit_tg_markdown(text: str, max_len: int) -> str:
    """Render text as MarkdownV2, clipping raw text until it fits Telegram."""
    rendered = _to_tg_markdown_v2(text)
    if len(rendered) <= max_len:
        return rendered
    suffix = "\n\n..."
    lo, hi = 0, len(text)
    best = _to_tg_markdown_v2(suffix.strip())
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _to_tg_markdown_v2(text[:mid].rstrip() + suffix)
        if len(candidate) <= max_len:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _chunk_for_telegram(text: str, max_len: int) -> list[str]:
    """Split text into messages that fit Telegram's 4096-char cap.

    Boundaries are tried in order of decreasing readability:
    paragraph (\\n\\n) → line (\\n) → sentence end → word → char.
    """
    if not text:
        return [" "]
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        cut = _find_split_point(remaining, max_len)
        head = remaining[:cut]
        stripped = head.rstrip()
        chunks.append(stripped or head)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _find_split_point(text: str, max_len: int) -> int:
    """Pick the best index ≤ max_len to cut `text` at."""
    window = text[:max_len]
    floor = max(max_len // 4, 1)

    idx = window.rfind("\n\n")
    if idx >= floor:
        return idx + 2

    idx = window.rfind("\n")
    if idx >= floor:
        return idx + 1

    sentence_idx = -1
    for terminator in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        i = window.rfind(terminator)
        if i != -1 and i + len(terminator) > sentence_idx:
            sentence_idx = i + len(terminator)
    if sentence_idx >= floor:
        return sentence_idx

    idx = window.rfind(" ")
    if idx >= floor:
        return idx + 1

    return max_len
