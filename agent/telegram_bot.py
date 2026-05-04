"""Telegram bot — forum-aware, multi-agent, per-lane sessions.

Each (chat_id, message_thread_id) pair is its own lane: per-lane claude/codex
session id, per-lane FIFO. All lanes share `/home/bux` as the working dir —
per-lane isolation is purely at the *agent session* layer (different session
UUIDs → different transcripts), not the filesystem. Within a lane messages
serialize so the same session UUID is never written by two procs at once.
Across lanes, no concurrency cap: spin up as many parallel claude/codex
turns as the box can carry.

Auth: deeplink-based one-shot setup token. First chat to redeem `/start <token>`
binds — every subsequent chat is silently dropped. Forum topics inside the
bound chat are auto-allowed.

Env (from /etc/bux/tg.env):
  TG_BOT_TOKEN     — Telegram bot token from @BotFather
  TG_SETUP_TOKEN   — random secret shown in the deeplink once; burns after first /start

State (on disk):
  /etc/bux/tg-allowed.txt        — newline-separated allowed chat_ids (mode 640 root:bux)
  /etc/bux/tg-state.json         — {offset, agents: {lane_slug: 'claude'|'codex'},
                                    owners: {chat_id: {user_id,name,username,bound_at}}}
  /etc/bux/tg-queue.json         — {lane_slug: [job, …]} pending FIFO
  /home/bux/.bux/sessions/<slug> — per-lane claude/codex session UUID
  /home/bux/workspaces/<slug>/   — per-lane cwd handed to the agent

Flow:
  1. Start → TG_BOT_TOKEN required; begin long-polling getUpdates.
  2. First message while TG_SETUP_TOKEN is set → bind the chat. Subsequent
     other-chat messages drop silently.
  3. Once allowed, each message is keyed to a lane (chat_id, thread_id) and
     enqueued. A worker drains that lane, dispatching each job to the lane's
     bound agent (claude default; `/codex` flips it).
  4. Stream-json events from claude come back as one editable TG message
     bubble in the lane's topic, with a per-turn random "thinking" emoji in
     the placeholder and a 💔 reaction only on failure.
"""

from __future__ import annotations

import errno
import fcntl
import grp
import json
import logging
import os
import pty
import pwd
import random
import re
import secrets
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from pathlib import Path

import httpx

LOG = logging.getLogger("bux-tg")

TG_ENV = Path("/etc/bux/tg.env")
BOX_ENV = Path("/etc/bux/env")
BROWSER_ENV = Path("/home/bux/.claude/browser.env")
OPENAI_ENV = Path("/home/bux/.secrets/openai.env")
ALLOWED_FILE = Path("/etc/bux/tg-allowed.txt")
STATE_FILE = Path("/etc/bux/tg-state.json")
QUEUE_FILE = Path("/etc/bux/tg-queue.json")

# Marker for "I've already told the user about this SHA". Lets transient
# bux-tg restarts (systemd flaps, polling backoff) stay silent while
# update-driven restarts (different SHA) announce themselves once.
LAST_ANNOUNCED_SHA = Path("/var/lib/bux/last-announced.sha")

# One-shot file written by /update before it kicks off bootstrap.sh. The next
# time the bot starts, _announce_online_if_new_sha reads this and unconditionally
# pings the listed lane(s) with a "back online" confirmation — even if the SHA
# didn't change and even if the lane was idle. The user explicitly asked for the
# restart, so they get a confirmation.  Format: one "<chat_id>\t<thread_id>" per
# line (thread_id may be empty). Consumed (deleted) after read.
UPDATE_REQUEST_LANES = Path("/var/lib/bux/update-request.lanes")

# Per-lane session-uuid root. Bot creates lazily as bux-owned.
SESSIONS_DIR = Path("/home/bux/.bux/sessions")
# Pre-lane "global" session, written by the legacy bot. We migrate this into
# the (chat_id, main) lane for the first chat that messages after upgrade so
# existing single-chat users keep their conversation history.
LEGACY_SESSION_FILE = Path("/home/bux/.bux/session")
# All lanes share this cwd. Per-lane isolation is via session UUID only —
# claude --resume <uuid> writes its transcript to
# ~/.claude/projects/-home-bux/<uuid>.jsonl, so different UUIDs in the same
# project dir give independent conversations without separate workspaces.
WORKSPACE = Path("/home/bux")

POLL_TIMEOUT = 30
REPLY_MAX = 3500  # TG's hard cap is 4096; keep headroom for reply trailers.

# Telegram bot API caps file downloads at 20 MB on the free tier — getFile
# returns ok but the subsequent download URL 404s past that. Bail early.
TG_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# Telegram delivers chat-lifecycle events (topic created, member joined,
# title changed, …) as regular Message updates with one of these payload
# fields set and no text/caption. They carry no user intent, so we drop
# them silently — otherwise creating a forum topic always bounces a
# "don't know how to handle that" reply at the user.
_SERVICE_MESSAGE_FIELDS = frozenset({
    "forum_topic_created",
    "forum_topic_edited",
    "forum_topic_closed",
    "forum_topic_reopened",
    "general_forum_topic_hidden",
    "general_forum_topic_unhidden",
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "message_auto_delete_timer_changed",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "pinned_message",
    "users_shared",
    "chat_shared",
    "write_access_allowed",
    "proximity_alert_triggered",
    "boost_added",
    "chat_background_set",
    "video_chat_scheduled",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_participants_invited",
    "giveaway_created",
    "giveaway",
    "giveaway_winners",
    "giveaway_completed",
    "web_app_data",
    "successful_payment",
    "refunded_payment",
    "connected_website",
    "passport_data",
})

# Failure reaction on the user's message. Telegram's free-tier reaction
# allowlist excludes ⏳/✅/⚠️/❌ — this is a verified-allowed pick.
EMOJI_ERROR = "💔"

# Pool the placeholder bubble draws from on every new turn — one random pick
# per `StreamingMessage.start()`. Goes inside a regular message body, not a
# reaction, so the Telegram reaction allowlist doesn't apply. Curated across
# approval / laughter / astonishment / doubt so the vibe stays "agent is on
# it" rather than going off into food or animals.
THINKING_EMOJIS = (
    # approval / cheering
    "👍", "🥰", "👏", "🙏", "👌", "💯", "🤝", "✨",
    "⭐", "🌟", "💪", "🤗", "✅", "🎯", "🥇", "🙌", "💫",
    "🎊", "🥳", "🫡", "🆒", "💘",
    # laughter / joy
    "😄", "😁", "😂", "🤣", "😆", "🤪", "😜", "😝", "🤭", "🥲",
    "😻", "🙃", "😋", "😎", "🥸", "😅", "🤠", "😇", "🤓", "🤡",
    # astonishment / wow
    "🤯", "😱", "🤩", "😲", "😮", "😯", "🫨", "🙀", "👀", "🫢",
    "🫣", "😳", "🥶", "🥵", "🌪", "💥", "🎆", "🎇",
    # doubt / thinking
    "🤔", "🤨", "🧐", "🤷", "🙄",
    "😶", "💭", "🐌", "🐢", "⏳", "🌀", "👻",
)


def random_thinking_emoji() -> str:
    """One placeholder emoji per turn. Cosmetic; never security-sensitive."""
    return random.choice(THINKING_EMOJIS)

# Recognised agents per lane. Values double as PATH binary names.
AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"
AGENTS = (AGENT_CLAUDE, AGENT_CODEX)
DEFAULT_AGENT = AGENT_CLAUDE


# Registered with Telegram via setMyCommands at boot. Order = order shown
# in the `/` autocomplete popup. Descriptions are short — TG clips them.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("help", "show all commands"),
    ("terminal", "open an interactive shell (e.g. /terminal gh auth login)"),
    ("exit", "close the active terminal session"),
    ("interrupt", "send Ctrl-C to the active terminal session"),
    ("enter", "send Enter to the active terminal session"),
    ("eof", "send Ctrl-D to the active terminal session"),
    ("compact", "summarize this topic's session to free up context"),
    ("agent", "switch this topic's agent (claude|codex)"),
    ("claude", "switch/login/logout Claude"),
    ("codex", "switch/login/logout Codex"),
    ("live", "live-view URL of the active browser"),
    ("queue", "pending tasks in this topic"),
    ("cancel", "kill the running task + drop pending"),
    ("schedules", "list reminders / cron jobs"),
    ("login", "auth status / connect a service (github/claude/codex)"),
    ("logout", "disconnect a service (e.g. /logout gh)"),
    ("invite", "mint a token to authorize a second chat"),
    ("whoami", "your TG identity + this lane's agent"),
    ("version", "show the bux agent version"),
    ("update", "pull latest code + restart"),
]


# ---------------------------------------------------------------------------
# MarkdownV2 helpers — Telegram's escape rules are strict and unforgiving.
# These are unchanged from the legacy bot; rendering rules don't depend on
# the lane / agent abstraction.
# ---------------------------------------------------------------------------

# Every char in this set must be backslash-escaped outside an entity, per
# https://core.telegram.org/bots/api#markdownv2-style. Inside ` ` and ``` ```
# only ` and \ are special.
_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_MDV2_ESCAPE = {c: "\\" + c for c in _MDV2_SPECIALS}


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
    inside the collapsed section. That keeps the renderer simple and
    avoids interactions between blockquote markup and code spans.

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


_STEP_SEPARATOR = "\n---------------\n"


def _build_header(total: int, shown: int, sub_agents: int, marker: str) -> str:
    """Compose the first (collapsed-visible) line of the blockquote.

    `<turn emoji> N messages` always. When trimming kicks in we extend with
    `(last K shown)` so the count remains honest about what's actually
    rendered. When sub-agents have been spawned we append
    ` · 🤖 +M sub-agents`. The indicator is omitted entirely when
    sub_agents == 0 so quiet turns stay quiet.
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

    `trailer`, if given, becomes the LAST line inside the blockquote,
    separated from the messages by a divider — used for the
    stats footer so it only shows up when the user expands the messages.
    Pass raw text; escaping happens in
    `_render_expandable_blockquote`.

    Returns "" for empty input. Trims OLDEST blocks until the rendered
    body fits under `max_body`; keeps at least the most recent one so
    something always renders.
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
    """Render every assistant text block as one collapsed blockquote.

    Used while the agent is still emitting — keeps the bubble compact on
    a chatty turn. The collapsed first line shows `<emoji> N messages` (plus a
    `🤖 +M sub-agents` suffix when applicable) so the user sees progress
    without expanding. Surviving blocks are separated by a
    `---------------` divider when expanded.
    """
    parts = [b.strip() for b in blocks if b and b.strip()]
    if not parts:
        return ""
    return _render_collapsed_steps(parts, len(parts), max_body, sub_agents=sub_agents, marker=marker)


def _humanize_tokens(n: int) -> str:
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.0f}M" if v >= 10 else f"{v:.1f}M"
    if n >= 1_000:
        v = n / 1_000
        return f"{v:.0f}k" if v >= 10 else f"{v:.1f}k"
    return str(n)


def _humanize_cached_input(cache_read: int, input_t: int) -> str:
    """Compact cached/total-input pair, e.g. 25/28M."""
    if input_t >= 1_000_000 or cache_read >= 1_000_000:
        return f"{cache_read / 1_000_000:.0f}/{input_t / 1_000_000:.0f}M"
    if input_t >= 1_000 or cache_read >= 1_000:
        return f"{cache_read / 1_000:.0f}/{input_t / 1_000:.0f}k"
    return f"{cache_read}/{input_t}"


def _humanize_duration_ms(ms: int) -> str:
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    m = s / 60
    if m < 60:
        return f"{m:.1f}m"
    return f"{m / 60:.1f}h"


def _normalize_codex_usage(usage: dict | None) -> dict | None:
    """Translate codex's usage shape into the keys _format_final_footer
    expects (which were originally claude's). Codex emits:
      { input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens }
    Footer expects:
      { input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens }
    Codex has no equivalent of cache_creation, so that stays unset.
    """
    if not isinstance(usage, dict):
        return None
    out: dict = {"provider": "codex"}
    if isinstance(usage.get("input_tokens"), int):
        out["input_tokens"] = usage["input_tokens"]
    if isinstance(usage.get("output_tokens"), int):
        out["output_tokens"] = usage["output_tokens"]
    if isinstance(usage.get("cached_input_tokens"), int):
        out["cache_read_input_tokens"] = usage["cached_input_tokens"]
    return out or None


def _count_claude_tool_uses(ev: dict) -> int:
    if ev.get("type") != "assistant":
        return 0
    content = (ev.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    )


def _is_codex_tool_event(ev: dict) -> bool:
    et = str(ev.get("type") or "").lower()
    item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
    item_type = str(item.get("type") or "").lower()
    if item_type in {
        "function_call",
        "custom_tool_call",
        "local_shell_call",
        "mcp_tool_call",
        "web_search_call",
    }:
        return True
    if "tool_call" in item_type or item_type.endswith("_call"):
        return "result" not in item_type and "output" not in item_type
    return et.endswith(".completed") and ("tool_call" in et or "function_call" in et)


def _format_final_footer(
    usage: dict | None,
    duration_ms: int | None,
    tool_calls: int = 0,
) -> str:
    """Build the raw stats line that goes at the bottom of the
    collapsed-steps blockquote. Returns "" if no data is available.
    Body is intentionally NOT MDV2-escaped — the caller passes it
    through `_render_expandable_blockquote`, which escapes lines.

    Format: `📥 cached/input A/B · 📤 output D · 🛠 tools · ⏱ duration`.
    Claude and Codex report input differently: Claude's input_tokens is
    uncached input only; Codex's input_tokens is total input with
    cached_input_tokens as a subset. Normalize before rendering.
    """
    parts: list[str] = []
    if isinstance(usage, dict):
        provider = usage.get("provider")
        input_t = usage.get("input_tokens")
        output_t = usage.get("output_tokens")
        cache_read = usage.get("cache_read_input_tokens")
        cache_create = usage.get("cache_creation_input_tokens")
        total_input = usage.get("total_input_tokens")
        if not isinstance(total_input, int) or total_input <= 0:
            if provider == "codex":
                # Codex reports input_tokens as total input and
                # cached_input_tokens as the cached subset.
                total_input = input_t if isinstance(input_t, int) and input_t > 0 else 0
            else:
                # Claude reports input_tokens as uncached input only.
                total_input = 0
                for n in (input_t, cache_read, cache_create):
                    if isinstance(n, int) and n > 0:
                        total_input += n
        if isinstance(total_input, int) and total_input > 0:
            if isinstance(cache_read, int) and cache_read > 0:
                parts.append(f"📥 cached/input {_humanize_cached_input(cache_read, total_input)}")
            else:
                parts.append(f"📥 input {_humanize_tokens(total_input)}")
        if isinstance(output_t, int) and output_t > 0:
            parts.append(f"📤 output {_humanize_tokens(output_t)}")
        if isinstance(cache_create, int) and cache_create > 0:
            parts.append(f"✨ cache-write {_humanize_tokens(cache_create)}")
    if tool_calls > 0:
        parts.append(f"🛠 {tool_calls}")
    if isinstance(duration_ms, int) and duration_ms > 0:
        parts.append(f"⏱ {_humanize_duration_ms(duration_ms)}")
    return " · ".join(parts)


def _render_final_view(
    blocks: list[str],
    max_body: int,
    sub_agents: int = 0,
    usage: dict | None = None,
    duration_ms: int | None = None,
    tool_calls: int = 0,
    marker: str = "💭",
) -> str:
    """Render the final-turn view: collapsed thinking trace ON TOP with a
    message-count header, final answer prominent BELOW.

    The stats footer lives INSIDE the collapsed
    blockquote, as the last line — so the user only sees it when they
    expand the messages. Keeps the visible main bubble (the answer) free
    of metadata while still surfacing the stats for those who want them.

    Heuristic: the last text block claude emitted is the answer; anything
    before it was thinking/narration. If there's only one block (a
    one-shot reply), no blockquote and no footer — just the answer.
    """
    parts = [b.strip() for b in blocks if b and b.strip()]
    if not parts:
        return ""
    final_md = _to_tg_markdown_v2(parts[-1])
    steps = parts[:-1]
    if not steps:
        # One-shot turn: skip the footer entirely. The user asked for
        # tokens/duration only when "there were some thinking messages so
        # it took a little bit longer."
        return final_md
    footer = _format_final_footer(usage, duration_ms, tool_calls=tool_calls)
    steps_max = max(max_body - len(final_md) - 2, 200)
    steps_md = _render_collapsed_steps(
        steps,
        len(steps),
        steps_max,
        sub_agents=sub_agents,
        trailer=footer,
        marker=marker,
    )
    if not steps_md:
        return final_md
    return steps_md + "\n\n" + final_md


def _chunk_for_telegram(text: str, max_len: int) -> list[str]:
    """Split on paragraph boundaries when possible so we don't slice
    mid-formatting (which TG would 400 on for MarkdownV2).
    Falls back to char-aligned cut for paragraphs longer than max_len."""
    if len(text) <= max_len:
        return [text or " "]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        if not current:
            current = para
        elif len(current) + 2 + len(para) <= max_len:
            current = current + "\n\n" + para
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    final: list[str] = []
    for c in chunks:
        if len(c) <= max_len:
            final.append(c)
        else:
            for i in range(0, len(c), max_len):
                final.append(c[i : i + max_len])
    return final


def _parse_command(text: str) -> tuple[str | None, str]:
    """Split a TG message into (command, argument) if it looks like a command.

    Telegram sends `/cmd@botname rest of arg` in group chats — strip the
    `@botname` suffix so the cmd matches whether the bot was invoked by
    bare /cancel or /cancel@bux_abcd1234_bot.

    Match `/cancel` as a token (not a prefix) so `/cancellation-policy` falls
    through to the agent-prompt path instead of being misread as cancel.
    """
    if not text or not text.startswith("/"):
        return None, ""
    parts = text.split(None, 1)
    head = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    cmd, _, _bot = head.partition("@")
    return cmd, rest


def _read_kv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _load_openai_key() -> str | None:
    """Read OPENAI_API_KEY from /home/bux/.secrets/openai.env.

    The file is mode 600 owned by bux; we run as root so we can read it.
    Returns None if missing/unset so callers can degrade gracefully (e.g.
    voice transcription answers "unavailable" rather than crashing the worker).
    """
    if not OPENAI_ENV.exists():
        return None
    return _read_kv(OPENAI_ENV).get("OPENAI_API_KEY") or None


def _extract_media(msg: dict) -> tuple[str | None, str, int]:
    """If `msg` carries a voice / audio / video_note, return
    (file_id, suggested_filename, file_size). Otherwise (None, '', 0).

    Whisper sniffs by extension, so we pick a plausible one based on which
    field carried the file: voice notes are opus-in-ogg, video_notes are
    mp4, audio uses whatever mime_type Telegram surfaced (with .mp3 as a
    safe default).
    """
    if "voice" in msg and isinstance(msg["voice"], dict):
        v = msg["voice"]
        return v.get("file_id"), "voice.ogg", int(v.get("file_size") or 0)
    if "video_note" in msg and isinstance(msg["video_note"], dict):
        v = msg["video_note"]
        return v.get("file_id"), "video_note.mp4", int(v.get("file_size") or 0)
    if "audio" in msg and isinstance(msg["audio"], dict):
        a = msg["audio"]
        fname = a.get("file_name") or ""
        mime = a.get("mime_type") or ""
        if fname and "." in fname:
            ext = fname.rsplit(".", 1)[1].lower()
        elif "mpeg" in mime or "mp3" in mime:
            ext = "mp3"
        elif "mp4" in mime or "m4a" in mime or "aac" in mime:
            ext = "m4a"
        elif "ogg" in mime or "opus" in mime:
            ext = "ogg"
        elif "wav" in mime:
            ext = "wav"
        else:
            ext = "mp3"
        return a.get("file_id"), f"audio.{ext}", int(a.get("file_size") or 0)
    return None, "", 0


# ---------------------------------------------------------------------------
# Allow-list + binding (verbatim from legacy bot).
# ---------------------------------------------------------------------------


def _load_openai_key() -> str | None:
    """Read OPENAI_API_KEY from /etc/bux/openai.env.

    Kept out of the systemd EnvironmentFile so it can be rotated without
    a service restart. Returns None when the file or key is missing so
    callers can degrade gracefully (friendly TG reply) instead of crashing
    the worker thread.
    """
    if not OPENAI_ENV.exists():
        return None
    kv = _read_kv(OPENAI_ENV)
    key = kv.get("OPENAI_API_KEY") or ""
    return key or None


def _extract_media(msg: dict) -> tuple[str | None, str, int]:
    """Detect a voice / audio / video_note attachment for transcription.

    Returns (file_id, suggested_filename, file_size). (None, '', 0) when
    the message has no transcribable media. Whisper sniffs by extension,
    so the filename matters: voice notes are opus-in-ogg, video_notes are
    mp4, audio uses whatever mime_type Telegram surfaced (mp3 fallback).
    """
    v = msg.get("voice")
    if isinstance(v, dict) and v.get("file_id"):
        return v["file_id"], "voice.ogg", int(v.get("file_size") or 0)
    vn = msg.get("video_note")
    if isinstance(vn, dict) and vn.get("file_id"):
        return vn["file_id"], "video_note.mp4", int(vn.get("file_size") or 0)
    a = msg.get("audio")
    if isinstance(a, dict) and a.get("file_id"):
        fname = a.get("file_name") or ""
        mime = a.get("mime_type") or ""
        if fname and "." in fname:
            ext = fname.rsplit(".", 1)[1].lower()
        elif "mpeg" in mime or "mp3" in mime:
            ext = "mp3"
        elif "mp4" in mime or "m4a" in mime or "aac" in mime:
            ext = "m4a"
        elif "ogg" in mime or "opus" in mime:
            ext = "ogg"
        elif "wav" in mime:
            ext = "wav"
        else:
            ext = "mp3"
        return a["file_id"], f"audio.{ext}", int(a.get("file_size") or 0)
    return None, "", 0


# Telegram message fields that mark a "service" event rather than user
# content. We drop these silently before any response logic — otherwise
# spinning up a new forum topic triggers our "unknown attachment" reply.
_SERVICE_MSG_KEYS = (
    "forum_topic_created",
    "forum_topic_edited",
    "forum_topic_closed",
    "forum_topic_reopened",
    "general_forum_topic_hidden",
    "general_forum_topic_unhidden",
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "pinned_message",
    "message_auto_delete_timer_changed",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_participants_invited",
    "video_chat_scheduled",
    "boost_added",
    "chat_shared",
    "users_shared",
    "write_access_allowed",
    "proximity_alert_triggered",
)


def _extract_sender(msg: dict) -> dict[str, str]:
    """Pull the sender's identity off a TG update.

    Group chats with multiple humans (after Forum Topics arrived) need
    per-message attribution: the agent should know whether the latest
    message came from the box owner or from a guest invited into the
    group. Returns an empty dict for service messages with no `from`.
    """
    src = msg.get("from") or {}
    user_id = src.get("id")
    username = (src.get("username") or "").strip()
    first = (src.get("first_name") or "").strip()
    last = (src.get("last_name") or "").strip()
    name = (first + " " + last).strip() or username or (str(user_id) if user_id else "")
    out: dict[str, str] = {}
    if user_id:
        out["user_id"] = str(user_id)
    if username:
        out["username"] = username
    if name:
        out["name"] = name
    return out


def _sender_label(sender: dict | None) -> str:
    """Human-readable `Name (@handle)` label, or '' if we have nothing."""
    if not sender:
        return ""
    name = sender.get("name") or ""
    handle = sender.get("username") or ""
    if name and handle and handle != name:
        return f"{name} (@{handle})"
    return name or (f"@{handle}" if handle else sender.get("user_id") or "")


def _prefix_sender(
    prompt: str,
    sender: dict | None,
    owner: dict | None = None,
) -> str:
    """Tag the prompt with `[from <Name>]` so the agent sees who's asking.

    Cheap natural-language hint rather than a schema change — the agent
    just reads the line and behaves accordingly. Skips silently when we
    have no sender info (service messages, edits, channel posts).

    When we know the chat's owner, append a soft role tag so the agent
    can tell whether the current sender is the box owner or a guest who
    joined the group later. No hard authorization gate — just context.

    Slash-command prompts (e.g. `/compact`) are passed through verbatim:
    claude's slash-command parser only fires when the prompt STARTS with
    `/`, and a `[from …]` prefix would leave the slash a few lines into
    the body, defeating the parse.
    """
    if prompt.startswith("/"):
        return prompt
    label = _sender_label(sender)
    if not label:
        return prompt
    role = ""
    if sender and owner and owner.get("user_id"):
        role = (
            ", the box owner"
            if str(sender.get("user_id") or "") == str(owner["user_id"])
            else ", a guest in this chat (not the box owner)"
        )
    return f"[from {label}{role}]\n\n{prompt}"


def _owner_for(chat_id: int, state: dict) -> dict | None:
    """Return the recorded owner for a chat, or None if not yet bound."""
    owners = state.get("owners") or {}
    rec = owners.get(str(chat_id))
    return rec if isinstance(rec, dict) else None


def _set_owner_for(chat_id: int, sender: dict, state: dict) -> None:
    """Record the binder as this chat's owner. First-binder-wins, no overwrite."""
    owners = state.setdefault("owners", {})
    if str(chat_id) in owners:
        return
    rec: dict = {"bound_at": int(time.time())}
    for k in ("user_id", "username", "name"):
        if sender.get(k):
            rec[k] = sender[k]
    if rec.get("user_id"):
        owners[str(chat_id)] = rec
        save_state(state)


def _is_owner(sender: dict | None, owner: dict | None) -> bool:
    if not sender or not owner:
        return False
    sid = str(sender.get("user_id") or "")
    oid = str(owner.get("user_id") or "")
    return bool(sid and oid and sid == oid)


def _box_owner(state: dict) -> dict | None:
    """Return the global box-owner record, or None if not yet bound.

    The "box owner" is the single human identity per box used to gate
    auto-allow of new chats and any other owner-only action. Resolved
    in priority order:

    1. `TG_OWNER_ID` in /etc/bux/tg.env (with optional TG_OWNER_USERNAME
       and TG_OWNER_NAME). Set by the install path when the cloud or
       operator provisioning the box already knows the human's TG
       identity. Authoritative — overrides everything else and prevents
       any first-message-wins race.
    2. Persisted `state["box_owner"]` from a previous bind.
    3. Derived from the earliest entry in `state["owners"]` (legacy
       migration for installs that pre-date the env var and box_owner
       fields). Result is persisted into state.

    Distinct from the per-chat `owners` map (which can have a different
    first-binder per chat in the multi-chat case).
    """
    env = _read_kv(TG_ENV)
    env_id = (env.get("TG_OWNER_ID") or "").strip()
    if env_id:
        rec: dict = {"user_id": env_id, "source": "env"}
        for src_key, dst_key in (("TG_OWNER_USERNAME", "username"), ("TG_OWNER_NAME", "name")):
            v = (env.get(src_key) or "").strip()
            if v:
                rec[dst_key] = v
        return rec
    rec = state.get("box_owner")
    if isinstance(rec, dict) and rec.get("user_id"):
        return rec
    owners = state.get("owners") or {}
    if not owners:
        return None
    earliest: dict | None = None
    for v in owners.values():
        if not isinstance(v, dict) or not v.get("user_id"):
            continue
        if earliest is None or (v.get("bound_at") or 0) < (earliest.get("bound_at") or 0):
            earliest = v
    if earliest:
        state["box_owner"] = {k: earliest[k] for k in ("user_id", "username", "name", "bound_at") if k in earliest}
        save_state(state)
    return state.get("box_owner")


def _set_box_owner(state: dict, sender: dict) -> None:
    """Record the global box owner. First-bind wins; never overwrite.

    Called from `_bind_chat` when the setup token gets redeemed. Once
    set, this identity gates auto-allow of new chats: only my_chat_member
    updates whose `from.id` matches this user_id will admit a fresh chat.
    """
    if state.get("box_owner"):
        return
    if not sender.get("user_id"):
        return
    rec: dict = {"bound_at": int(time.time())}
    for k in ("user_id", "username", "name"):
        if sender.get(k):
            rec[k] = sender[k]
    state["box_owner"] = rec
    save_state(state)


def load_allow() -> set[int]:
    if not ALLOWED_FILE.exists():
        return set()
    return {int(x) for x in ALLOWED_FILE.read_text().split() if x.strip()}


def _parse_lane_slug(slug: str) -> LaneKey | None:
    """Parse `<chat>_main` or `<chat>_<thread>` lane slugs from disk state."""
    if not slug:
        return None
    if slug.endswith("_main"):
        chat = slug[: -len("_main")]
        try:
            return (int(chat), 0)
        except ValueError:
            return None
    try:
        chat, thread = slug.rsplit("_", 1)
        return (int(chat), int(thread))
    except ValueError:
        return None


def _chmod_root_bux_640(path: Path) -> None:
    """Set `path` to 0o640 root:bux. Raises on failure.

    Used for /etc/bux/tg.env and /etc/bux/tg-allowed.txt — both need to be
    readable by the bux user so the `tg-send` helper (running as bux from
    at/cron / claude bash tool) can post to TG. Fail loud rather than leave
    a silently-unreadable file.
    """
    bux_gid = grp.getgrnam("bux").gr_gid
    os.chown(path, 0, bux_gid)
    path.chmod(0o640)


def add_allow(chat_id: int) -> None:
    ids = load_allow() | {chat_id}
    ALLOWED_FILE.write_text("\n".join(str(i) for i in sorted(ids)))
    _chmod_root_bux_640(ALLOWED_FILE)


def burn_setup_token() -> None:
    """Remove TG_SETUP_TOKEN from /etc/bux/tg.env after first successful bind.

    Single-use: once a chat_id is bound, the setup token is useless and
    should not sit on disk. A breach/backup leak then can't bind a new chat.
    """
    if not TG_ENV.exists():
        return
    kept: list[str] = []
    for line in TG_ENV.read_text().splitlines():
        if line.strip().startswith("TG_SETUP_TOKEN="):
            continue
        kept.append(line)
    TG_ENV.write_text("\n".join(kept) + ("\n" if kept else ""))
    _chmod_root_bux_640(TG_ENV)


# ---------------------------------------------------------------------------
# State file — getUpdates offset + per-lane agent binding.
# Format: {"offset": <int>, "agents": {"<lane_slug>": "claude"|"codex"}}
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, dict):
                data.setdefault("offset", 0)
                data.setdefault("agents", {})
                data.setdefault("owners", {})
                return data
        except Exception:
            pass
    return {"offset": 0, "agents": {}, "owners": {}}


def save_state(s: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    tmp.replace(STATE_FILE)
    try:
        STATE_FILE.chmod(0o600)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Lane infrastructure.
# A lane is the unit of message ordering + agent state. (chat_id, thread_id)
# where thread_id is 0 for non-forum chats and the message_thread_id from
# Telegram for forum topics.
# ---------------------------------------------------------------------------

LaneKey = tuple[int, int]


def _lane_key(chat_id: int, thread_id: int | None) -> LaneKey:
    return (chat_id, thread_id or 0)


def _lane_slug(key: LaneKey) -> str:
    chat, thread = key
    return f"{chat}_main" if not thread else f"{chat}_{thread}"


def _bux_uid_gid() -> tuple[int, int]:
    bux = pwd.getpwnam("bux")
    return bux.pw_uid, bux.pw_gid


def _ensure_bux_dir(path: Path) -> Path:
    """Create `path` (and parents) bux-owned. Idempotent.

    The bot runs as root; agents run as bux. Without explicit chown the dir
    tree ends up root-owned and the bux-side processes get permission errors
    on first read/write. Pre-create the parent chain so we don't keep root-
    owned ancestors above bux-owned leaves.
    """
    uid, gid = _bux_uid_gid()
    # Walk up and chown any segment we ourselves create.
    to_create: list[Path] = []
    cur = path
    while not cur.exists():
        to_create.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    for p in reversed(to_create):
        p.mkdir(mode=0o755, exist_ok=True)
        try:
            os.chown(p, uid, gid)
        except Exception:
            LOG.exception("chown %s failed", p)
    return path


def _codex_thread_id_for(key: LaneKey) -> str | None:
    """Return the persisted codex thread_id for this lane, or None.

    Codex emits `thread.started` on the first `codex exec --json` call;
    we capture that id and persist it so subsequent turns in the same
    lane can `codex exec resume <id>` and keep conversation context.
    Returns None on first message (no thread yet) — caller falls back
    to a plain `codex exec --json` and saves the new id afterwards.
    """
    _ensure_bux_dir(SESSIONS_DIR)
    f = SESSIONS_DIR / f"{_lane_slug(key)}.codex"
    if not f.exists():
        return None
    try:
        fd = os.open(str(f), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(fd, "r") as fh:
                tid = fh.read().strip()
        except Exception:
            os.close(fd)
            raise
        return tid or None
    except OSError as e:
        LOG.warning("reading %s failed (%s); treating as fresh", f, e)
        return None


def _save_codex_thread_id(key: LaneKey, thread_id: str) -> None:
    """Persist the codex thread_id for this lane (atomic, symlink-safe)."""
    if not thread_id:
        return
    f = SESSIONS_DIR / f"{_lane_slug(key)}.codex"
    _write_session_uuid(f, thread_id)


def _claude_session_flag(sid: str) -> list[str]:
    """Pick `--session-id` (create) vs `--resume` (continue) for `sid`.

    Claude Code 2.x rejects `--session-id <existing>` with "Session ID is
    already in use" — it's create-only. We detect prior creation by looking
    for the transcript file claude writes at
    `~/.claude/projects/-home-bux/<sid>.jsonl` after the first turn (WORKSPACE
    is /home/bux, encoded as `-home-bux`). Existing transcript → resume; no
    transcript → create.
    """
    transcript = Path("/home/bux/.claude/projects/-home-bux") / f"{sid}.jsonl"
    return ["--resume", sid] if transcript.exists() else ["--session-id", sid]


def _session_uuid_for(key: LaneKey) -> str:
    """Return the per-lane claude session UUID, persisting on first call.

    On first call for the (chat_id, 0) lane after the legacy bot, we migrate
    the global /home/bux/.bux/session into the per-lane file so the user's
    existing claude conversation continues seamlessly.
    """
    _ensure_bux_dir(SESSIONS_DIR)
    per_lane = SESSIONS_DIR / _lane_slug(key)
    # Existing per-lane file — read with O_NOFOLLOW. The bot is root and the
    # parent dir is bux-writable; we don't want to follow a planted symlink.
    if per_lane.exists():
        try:
            fd = os.open(str(per_lane), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with os.fdopen(fd, "r") as f:
                    sid = f.read().strip()
            except Exception:
                os.close(fd)
                raise
            if len(sid) == 36 and sid.count("-") == 4:
                return sid
        except OSError as e:
            LOG.warning("reading %s failed (%s); regenerating", per_lane, e)
    # Migrate the legacy global session into the (chat, main) slot if present.
    if key[1] == 0 and LEGACY_SESSION_FILE.exists():
        try:
            fd = os.open(str(LEGACY_SESSION_FILE), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with os.fdopen(fd, "r") as f:
                    sid = f.read().strip()
            except Exception:
                os.close(fd)
                raise
            if len(sid) == 36 and sid.count("-") == 4:
                _write_session_uuid(per_lane, sid)
                # Drop the legacy file so a future regen of the per-lane file
                # (manual delete, disk corruption) doesn't silently re-migrate
                # the same UUID and clobber whatever fresh session was written.
                try:
                    LEGACY_SESSION_FILE.unlink()
                except Exception:
                    LOG.exception("legacy session unlink failed")
                LOG.info("migrated legacy session %s → lane %s", sid, _lane_slug(key))
                return sid
        except OSError as e:
            LOG.warning("legacy session read failed (%s); minting fresh", e)
    # Fresh.
    sid = str(uuid.uuid4())
    _write_session_uuid(per_lane, sid)
    return sid


def _write_session_uuid(path: Path, sid: str) -> None:
    """Write `sid` to `path` symlink-safely and chown to bux.

    The bot is root; the bux user owns /home/bux. A symlink at `path`
    pointing at /etc/shadow would otherwise let bux trick us into clobbering
    system files. O_NOFOLLOW + O_EXCL covers create; lchown beats symlink
    resolution on the chown half.
    """
    if path.parent.is_symlink():
        raise RuntimeError(f"{path.parent} is a symlink; refusing to write session")
    # Atomic create-or-overwrite via rename. Tmp under same dir for atomicity.
    tmp = path.with_suffix(".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(
        str(tmp),
        os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_NOFOLLOW,
        0o644,
    )
    try:
        os.write(fd, sid.encode())
    finally:
        os.close(fd)
    tmp.replace(path)
    try:
        uid, gid = _bux_uid_gid()
        os.lchown(path, uid, gid)
    except Exception:
        LOG.exception("chown %s failed", path)


def _agent_for(key: LaneKey, state: dict) -> str:
    """Resolve which agent handles this lane. /agent <claude|codex> sets it."""
    bound = (state.get("agents") or {}).get(_lane_slug(key))
    if bound in AGENTS:
        return bound
    return DEFAULT_AGENT


def _set_agent_for(key: LaneKey, agent: str, state: dict) -> None:
    if agent not in AGENTS:
        return
    state.setdefault("agents", {})[_lane_slug(key)] = agent
    save_state(state)


# ---------------------------------------------------------------------------
# Per-lane FIFO + worker pool.
#
# Each lane has its own queue (in-memory, mirrored to QUEUE_FILE). A lane
# worker thread drains its lane and exits when empty; the next enqueue
# spawns a fresh worker. No cross-lane concurrency cap — every active topic
# can run in parallel. No wall-clock cap either: an agent can run for
# hours if it needs to (long browser flows, deep research, big edits).
# A genuinely-stuck claude pid sits forever until the user kills it from
# ssh; that cost is local to one topic, the others keep working.
#
# Within a lane, jobs serialize — same agent session UUID, no concurrent
# writes from two procs against the same on-disk transcript.
# ---------------------------------------------------------------------------

# Job shape:
#   id        — short hex (token for /cancel <id>)
#   chat_id   — TG chat (== lane_key[0])
#   thread_id — TG thread (== lane_key[1]); 0 means "no topic" (chat root)
#   message_id — TG message id (so we can `reply_to` the original)
#   prompt    — user's text
#   queued_at — unix ts
#   status    — 'queued' | 'in_flight'
#
# 'in_flight' rows on startup are dropped (worker died mid-task; we can't
# tell if the agent finished, so we don't retry — user resends if needed).

_lanes_lock = threading.Lock()
_lanes: dict[str, list[dict]] = {}  # lane_slug → [job, ...]
_lane_workers: dict[str, threading.Thread] = {}

# Per-lane handle to the in-flight agent subprocess (claude or codex), keyed
# by `_lane_slug(key)`. `/cancel` reads this under `_inflight_lock` and sends
# SIGKILL to unblock the lane when the agent shells into something
# interactive (gh auth login, vim, ssh, psql) and wedges on stdin. The entry
# is added right after Popen succeeds and removed in `finally` so an early
# error path can't strand a stale handle.
_inflight_procs: dict[str, subprocess.Popen] = {}
_inflight_lock = threading.Lock()


def _new_job_id() -> str:
    """8 hex chars. Short enough to type into /cancel <id>, long enough that
    collisions across the queue are essentially impossible."""
    return secrets.token_hex(4)


def _load_lanes_from_disk() -> dict[str, list[dict]]:
    """Load tg-queue.json. Tolerates the legacy flat-list shape (pre-lanes)."""
    if not QUEUE_FILE.exists():
        return {}
    try:
        raw = QUEUE_FILE.read_text()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        LOG.exception("reading %s failed; starting empty", QUEUE_FILE)
        return {}
    # Legacy shape: a flat list of jobs from before forum lanes existed.
    # Promote each into its (chat_id, 0) lane so we don't lose pending work.
    if isinstance(data, list):
        out: dict[str, list[dict]] = {}
        for j in data:
            if not isinstance(j, dict):
                continue
            chat = j.get("chat_id")
            if not isinstance(chat, int):
                continue
            j.setdefault("thread_id", 0)
            out.setdefault(_lane_slug((chat, 0)), []).append(j)
        LOG.info("migrated %d legacy jobs into lanes", sum(len(v) for v in out.values()))
        return out
    if isinstance(data, dict):
        clean: dict[str, list[dict]] = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, list):
                clean[k] = [j for j in v if isinstance(j, dict)]
        return clean
    return {}


def _save_lanes_to_disk_locked() -> None:
    """Caller must hold _lanes_lock. Atomic rename to avoid half-files."""
    tmp = QUEUE_FILE.with_suffix(".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(
        str(tmp),
        os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.write(fd, json.dumps(_lanes).encode())
    finally:
        os.close(fd)
    tmp.replace(QUEUE_FILE)


def _lanes_init() -> None:
    """Hydrate from disk; scrub stale in_flight rows (worker died last run)."""
    global _lanes
    with _lanes_lock:
        _lanes = _load_lanes_from_disk()
        dropped = 0
        for slug, jobs in list(_lanes.items()):
            kept = [j for j in jobs if j.get("status") != "in_flight"]
            dropped += len(jobs) - len(kept)
            if kept:
                _lanes[slug] = kept
            else:
                _lanes.pop(slug, None)
        if dropped:
            LOG.warning("dropped %d stale in_flight job(s) from previous run", dropped)
        _save_lanes_to_disk_locked()


def _spawn_lane_worker_locked(slug: str, run_drain) -> None:
    """Caller holds _lanes_lock. Idempotent: no-op if a worker exists."""
    if slug in _lane_workers:
        return
    t = threading.Thread(
        target=run_drain,
        args=(slug,),
        name=f"lane-{slug}",
        daemon=True,
    )
    _lane_workers[slug] = t
    t.start()


def _enqueue(slug: str, job: dict, run_drain) -> int:
    """Push job onto its lane's queue. Returns new lane depth.
    Spawns a worker if the lane has none alive (caller passes the drain fn)."""
    with _lanes_lock:
        _lanes.setdefault(slug, []).append(job)
        _save_lanes_to_disk_locked()
        depth = len(_lanes[slug])
        _spawn_lane_worker_locked(slug, run_drain)
    return depth


def _resume_pending_workers(run_drain) -> int:
    """Spawn drain threads for every lane that already has queued work.

    Called once at boot, after `_lanes_init()` rehydrates from disk. Without
    this, leftover queued jobs from a previous run would sit dormant until
    the next user message landed in that exact lane — and the boot-time
    "fully ready" announcement would never fire because nobody is draining
    the snapshot. Returns lane count for logging.
    """
    with _lanes_lock:
        slugs = [slug for slug, jobs in _lanes.items() if jobs]
        for slug in slugs:
            _spawn_lane_worker_locked(slug, run_drain)
    return len(slugs)


def _snapshot_pending_job_ids() -> set[str]:
    """Snapshot every job id currently in the lane state. Used at boot to
    track when the leftover work from a previous run has fully drained."""
    with _lanes_lock:
        return {
            j.get("id")
            for jobs in _lanes.values()
            for j in jobs
            if isinstance(j.get("id"), str)
        }


def _snapshot_pending_by_lane() -> dict[LaneKey, set[str]]:
    """Same snapshot as _snapshot_pending_job_ids, but bucketed by lane.

    Boot announcement uses this to target only the lanes whose work was
    interrupted by the restart — idle lanes don't need a notification.
    """
    out: dict[LaneKey, set[str]] = {}
    with _lanes_lock:
        for slug, jobs in _lanes.items():
            ids = {j.get("id") for j in jobs if isinstance(j.get("id"), str)}
            if not ids:
                continue
            key = _parse_lane_slug(slug)
            if key is None:
                continue
            out.setdefault(key, set()).update(ids)
    return out


def _pop_next_locked(slug: str) -> dict | None:
    """Caller holds _lanes_lock. Atomically flip head from queued → in_flight."""
    q = _lanes.get(slug, [])
    for j in q:
        if j.get("status") == "queued":
            j["status"] = "in_flight"
            _save_lanes_to_disk_locked()
            return j
    return None


def _finish_locked(slug: str, job_id: str) -> None:
    """Remove a finished job and clean up the lane / worker entry if empty."""
    q = _lanes.get(slug, [])
    for i, j in enumerate(q):
        if j.get("id") == job_id:
            q.pop(i)
            break
    if not q:
        _lanes.pop(slug, None)
    _save_lanes_to_disk_locked()


def _remove_queued(slug: str, job_id: str) -> dict | None:
    """User /cancel — drop a queued (not in_flight) job. Returns row or None."""
    with _lanes_lock:
        q = _lanes.get(slug, [])
        for i, j in enumerate(q):
            if j.get("id") == job_id:
                if j.get("status") != "queued":
                    return j
                q.pop(i)
                if not q:
                    _lanes.pop(slug, None)
                _save_lanes_to_disk_locked()
                return j
        return None


def _drop_all_queued(slug: str) -> int:
    """Bare /cancel — drop everything queued (not in_flight) in this lane."""
    with _lanes_lock:
        q = _lanes.get(slug, [])
        before = len(q)
        kept = [j for j in q if j.get("status") == "in_flight"]
        dropped = before - len(kept)
        if kept:
            _lanes[slug] = kept
        else:
            _lanes.pop(slug, None)
        _save_lanes_to_disk_locked()
        return dropped


def _snapshot_lane(slug: str) -> list[dict]:
    with _lanes_lock:
        return [dict(j) for j in _lanes.get(slug, [])]


def _lane_promote_to_front(slug: str, job_id: str) -> dict | None:
    """Steer a queued job to the head of its lane's queue (just after any
    in_flight job, so the next `_pop_next_locked` call returns it).

    Returns the moved job on success, or None when the job isn't found OR
    isn't queued anymore (already in_flight / already finished). The
    callback handler uses None to render an "already running" toast so a
    stale Steer tap can't quietly do nothing or, worse, kill the very job
    the user wanted to promote.
    """
    with _lanes_lock:
        q = _lanes.get(slug, [])
        for i, j in enumerate(q):
            if j.get("id") != job_id:
                continue
            if j.get("status") != "queued":
                return None
            target = 0
            for k, jk in enumerate(q):
                if jk.get("status") == "in_flight":
                    target = k + 1
            if i == target:
                return j
            q.pop(i)
            q.insert(target, j)
            _save_lanes_to_disk_locked()
            return j
        return None


def _lane_set_steer_msg_id(slug: str, job_id: str, msg_id: int) -> None:
    """Persist the message_id of the queue-ack bubble that hosts the Steer
    button. The lane drainer reads this when the job naturally starts so
    it can edit the keyboard off — otherwise a stale Steer tap on a now-
    running job would no-op (we'd see status=in_flight) but the button
    would still be visible, which is confusing."""
    with _lanes_lock:
        for j in _lanes.get(slug, []):
            if j.get("id") == job_id:
                j["steer_msg_id"] = int(msg_id)
                _save_lanes_to_disk_locked()
                return


# ---------------------------------------------------------------------------
# `/terminal` — interactive shell mode.
#
# Owner sends `/terminal` (optionally with an initial command, e.g.
# `/terminal gh auth login`). The bot spawns a persistent `bash` as the
# bux user inside a PTY and streams output back to the lane. From that
# moment on, every plain-text message in the lane is written to the
# shell's stdin (newline-appended) — so an interactive login flow
# (codex / gh device codes, sudo passwords, npx prompts) just works:
# the URL / prompt lands in TG, the user pastes the answer as a normal
# message, the bot routes it back to the running shell. `/exit` (or
# typing `exit` as plain text) ends the session once bash receives it.
# `/interrupt` sends Ctrl-C to the foreground process, `/eof` sends Ctrl-D,
# and `/cancel` SIGKILLs the session group as the hard escape hatch.
#
# `/terminal` while a session is already active is a no-op so a typo
# doesn't tear down what the user is working on. Bypasses the lane
# FIFO: terminal work is meant for the user to do now.
# ---------------------------------------------------------------------------

# Strip ANSI CSI / OSC sequences so colors / cursor moves don't show up as
# literal `\x1b[31m` garbage on the phone screen.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_URL_RE = re.compile(r"https?://[^\s<>()]+")
_DEVICE_CODE_RE = re.compile(r"\b(?:[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+|[A-Z0-9]{6,12})\b")

# Output buffering: flush after N bytes accumulated OR M seconds of quiet.
# Phone-screen friendly bubble; large enough that `npm install` doesn't
# spam ten bubbles per tick.
_SHELL_FLUSH_BYTES = 2500
_SHELL_FLUSH_QUIET_SEC = 0.8

_shell_sessions_lock = threading.Lock()
_shell_sessions: dict[str, "ShellSession"] = {}  # lane_slug → session


def _get_shell_session(slug: str) -> "ShellSession | None":
    with _shell_sessions_lock:
        sess = _shell_sessions.get(slug)
        if sess is not None and not sess.alive:
            _shell_sessions.pop(slug, None)
            return None
        return sess


class ShellSession:
    """One owner-driven persistent bash session inside a PTY, scoped to a lane.

    Lifecycle:
      - start() forks `bash` as bux inside a PTY. If `initial_cmd` is set,
        it's written to bash's stdin right after the PTY is hooked up so
        the user can do `/terminal gh auth login` as a single message.
      - A reader thread pulls from the master fd, strips ANSI, and posts
        debounced chunks to TG as monospace bubbles.
      - send_input(text) writes user-supplied text + "\\n" to the master
        fd. Plain-text messages in the lane go through this path.
      - The user types `exit` (or sends Ctrl-D) → bash exits → reader sees
        EOF → teardown posts a final exit-code bubble. `/exit`,
        `/interrupt`, `/eof`, and `/cancel` are server-side fast paths:
        graceful shell exit, Ctrl-C, Ctrl-D, and SIGKILL respectively.

    Only one session per lane. `/terminal` while one is alive is a no-op.
    """

    def __init__(
        self,
        bot: "Bot",
        chat_id: int,
        thread_id: int,
        slug: str,
        initial_cmd: str | None = None,
        reply_to: int | None = None,
        auto_enter_after_input_sec: float | None = None,
        close_on_success_patterns: tuple[str, ...] = (),
        success_message: str | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.slug = slug
        self.initial_cmd = initial_cmd
        self.reply_to = reply_to
        self.auto_enter_after_input_sec = auto_enter_after_input_sec
        self.close_on_success_patterns = tuple(p.lower() for p in close_on_success_patterns)
        self.success_message = success_message
        self.master_fd: int | None = None
        self.process: subprocess.Popen | None = None
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._last_flush = time.time()
        self._stop = threading.Event()
        self.alive = False
        self.started_at = 0.0
        self._reader_thread: threading.Thread | None = None
        self._flusher_thread: threading.Thread | None = None
        self._announced_urls: set[str] = set()
        self._success_close_started = False

    # -- spawn ---------------------------------------------------------------

    def start(self) -> None:
        master_fd, slave_fd = pty.openpty()
        # 80x24 is what most CLIs assume. Some auth flows render goofily
        # if the columns are too narrow.
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        except Exception:
            pass

        # Persistent bash as the bux user with login env so `npm`, `codex`,
        # `gh`, etc. find their config and creds. `sudo -iu bux -- bash -i`
        # mirrors what the user gets from `sudo -iu bux` on ttyd / ssh.
        # Force a minimal PS1 so we don't spam the lane with multi-line
        # color prompts on every command — the bash itself still echoes
        # input and prints output, which is the part that matters.
        argv = [
            "sudo", "-iu", "bux", "--",
            "bash", "-i",
        ]

        def _make_session_leader() -> None:
            # Run in the child between fork() and exec(): start a new
            # session (so kill() can target the whole group), and make
            # the slave PTY the controlling terminal so bash gets job
            # control and doesn't print "cannot set terminal process
            # group" / "no job control in this shell" on startup.
            os.setsid()
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        try:
            proc = subprocess.Popen(
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=_make_session_leader,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        # Parent doesn't need the slave end. Closing it here lets the
        # reader notice EOF on master when the child exits.
        os.close(slave_fd)

        # Make master non-blocking so the reader thread can poll without
        # wedging on a child that's waiting for input.
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self.master_fd = master_fd
        self.process = proc
        self.alive = True
        self.started_at = time.time()

        with _shell_sessions_lock:
            _shell_sessions[self.slug] = self

        ack_lines = [
            "💻 *terminal session started*",
            "_replies go straight to the shell — /interrupt sends Ctrl-C, "
            "/enter sends a blank Enter, /exit asks bash to close, /cancel hard-kills_",
        ]
        if self.initial_cmd:
            ack_lines.append(f"running: `{self.initial_cmd}`")
        self.bot.send(
            self.chat_id,
            "\n".join(ack_lines),
            reply_to=self.reply_to,
            thread_id=self.thread_id,
            markdown=True,
        )

        self._reader_thread = threading.Thread(
            target=self._reader,
            name=f"shell-reader-{self.slug}",
            daemon=True,
        )
        self._flusher_thread = threading.Thread(
            target=self._flusher,
            name=f"shell-flusher-{self.slug}",
            daemon=True,
        )
        self._reader_thread.start()
        self._flusher_thread.start()

        # Seed an initial command if the user typed `/terminal <cmd>`. The
        # write happens after the threads start so the bash startup output
        # and the seeded command's output both stream back as one flow.
        if self.initial_cmd:
            try:
                os.write(self.master_fd, (self.initial_cmd + "\n").encode("utf-8", "replace"))
            except Exception:
                LOG.exception("seeding initial cmd for shell session %s failed", self.slug)

    # -- input ---------------------------------------------------------------

    def send_input(self, text: str) -> bool:
        """Write `text` (newline-appended) to the PTY master. Returns False
        if the session has already exited. The PTY's own line-discipline
        handles echoing what we write back into the read stream, so we
        don't echo in user space — that would double-print every line."""
        if not self.alive or self.master_fd is None:
            return False
        payload = (text + "\n").encode("utf-8", "replace")
        try:
            os.write(self.master_fd, payload)
            if self.auto_enter_after_input_sec is not None:
                self._schedule_auto_enter()
            return True
        except OSError as e:
            LOG.warning("shell stdin write failed for %s: %s", self.slug, e)
            return False

    def _schedule_auto_enter(self) -> None:
        def _send() -> None:
            if self.alive:
                self.send_bytes(b"\n")

        timer = threading.Timer(self.auto_enter_after_input_sec, _send)
        timer.daemon = True
        timer.start()

    def send_bytes(self, payload: bytes) -> bool:
        """Write raw bytes to the PTY master. Used for terminal control
        characters like Ctrl-C and Ctrl-D."""
        if not self.alive or self.master_fd is None:
            return False
        try:
            os.write(self.master_fd, payload)
            return True
        except OSError as e:
            LOG.warning("shell control write failed for %s: %s", self.slug, e)
            return False

    # -- kill ----------------------------------------------------------------

    def kill(self, reason: str = "cancelled") -> None:
        """SIGKILL the process group and tear down the session."""
        if not self.alive:
            return
        proc = self.process
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                LOG.exception("killpg for shell session %s failed", self.slug)
        self._stop.set()
        # Reader will notice EOF on the master fd and finish; the closer
        # path runs in `_reader` so we don't double-close here.

    # -- reader / flusher ----------------------------------------------------

    def _reader(self) -> None:
        assert self.master_fd is not None
        try:
            while not self._stop.is_set():
                try:
                    rlist, _, _ = select.select([self.master_fd], [], [], 0.5)
                except (ValueError, OSError):
                    break
                if not rlist:
                    continue
                try:
                    chunk = os.read(self.master_fd, 4096)
                except OSError as e:
                    # EIO when the slave end is gone (child exited).
                    if e.errno in (errno.EIO,):
                        break
                    if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        continue
                    LOG.warning("read on shell master failed for %s: %s", self.slug, e)
                    break
                if not chunk:
                    break
                with self._buffer_lock:
                    self._buffer.extend(chunk)
        finally:
            self._teardown()

    def _flusher(self) -> None:
        while not self._stop.is_set() and self.alive:
            time.sleep(0.2)
            self._maybe_flush(force=False)
        # Final flush after teardown, in case the reader's last chunk
        # hadn't yet been pushed.
        self._maybe_flush(force=True)

    def _maybe_flush(self, force: bool) -> None:
        with self._buffer_lock:
            if not self._buffer:
                return
            quiet_for = time.time() - self._last_flush
            big_enough = len(self._buffer) >= _SHELL_FLUSH_BYTES
            old_enough = quiet_for >= _SHELL_FLUSH_QUIET_SEC
            if not (force or big_enough or old_enough):
                return
            payload = bytes(self._buffer)
            self._buffer.clear()
            self._last_flush = time.time()

        text = payload.decode("utf-8", "replace")
        text = _ANSI_RE.sub("", text)
        # Compact carriage returns so progress bars don't fill the bubble.
        text = re.sub(r"\r\n", "\n", text)
        text = re.sub(r"[^\n]*\r", "", text)
        text = text.rstrip("\n")
        if not text:
            return
        # Code blocks preserve terminal formatting, but Telegram may not make
        # URLs inside them tappable. Surface each new URL once as a plain
        # message so auth links from Claude/GitHub/etc. are easy to open.
        codex_code = ""
        codex_match = _DEVICE_CODE_RE.search(text)
        if codex_match:
            codex_code = codex_match.group(0)
        for match in _URL_RE.finditer(text):
            url = match.group(0).rstrip(".,;:")
            if url in self._announced_urls:
                continue
            self._announced_urls.add(url)
            if "auth.openai.com/codex/device" in url and codex_code:
                self.bot.send(
                    self.chat_id,
                    "Open this Codex device-auth link:\n"
                    f"{url}\n\n"
                    "Enter this one-time code:\n"
                    f"`{codex_code}`\n\n"
                    "If OpenAI asks you to enable this sign-in method in "
                    "security settings, enable it there, then press "
                    "*Retry after enabling* below to restart the flow.",
                    thread_id=self.thread_id,
                    markdown=True,
                    reply_markup=_codex_login_reply_markup(url, codex_code),
                )
                continue
            self.bot.send(
                self.chat_id,
                url,
                thread_id=self.thread_id,
                reply_markup=_url_reply_markup(url),
            )
        # Split into chunks that fit a TG message; each goes as its own
        # monospace bubble. Leaves a margin for the ``` fences. Escape
        # backslashes / backticks per MDV2 code-block rules — pre_rendered
        # skips the converter so we have to do it ourselves.
        for chunk in _split_into_code_bubbles(text, max_chars=3500):
            escaped = _escape_mdv2_code(chunk)
            self.bot.send(
                self.chat_id,
                f"```\n{escaped}\n```",
                thread_id=self.thread_id,
                pre_rendered=True,
            )
        self._maybe_close_after_success(text)

    def _maybe_close_after_success(self, text: str) -> None:
        if self._success_close_started or not self.close_on_success_patterns:
            return
        low = text.lower()
        if not any(p in low for p in self.close_on_success_patterns):
            return
        self._success_close_started = True
        if self.success_message:
            self.bot.send(
                self.chat_id,
                self.success_message,
                thread_id=self.thread_id,
                markdown=True,
            )
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, b"exit\n")
            except OSError:
                self.kill(reason="login-success")

    def _teardown(self) -> None:
        # Idempotent — both reader EOF and kill() can land here.
        if not self.alive:
            return
        self.alive = False
        self._stop.set()
        proc = self.process
        rc: int | None = None
        if proc is not None:
            try:
                rc = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    rc = proc.wait(timeout=1)
                except Exception:
                    rc = None
            except Exception:
                rc = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
            self.master_fd = None
        with _shell_sessions_lock:
            if _shell_sessions.get(self.slug) is self:
                _shell_sessions.pop(self.slug, None)
        # Final flush of whatever the reader had left over.
        self._maybe_flush(force=True)

        # Map common exit codes to a friendlier emoji.
        if rc is None:
            footer = "💻 _terminal closed — lane back to agent_"
        elif rc == 0:
            footer = "✅ _terminal closed (exit 0) — lane back to agent_"
        elif rc < 0:
            footer = f"🛑 _terminal killed (signal {-rc}) — lane back to agent_"
        else:
            footer = f"❌ _terminal closed (exit {rc}) — lane back to agent_"
        try:
            self.bot.send(
                self.chat_id,
                footer,
                thread_id=self.thread_id,
                markdown=True,
            )
        except Exception:
            LOG.exception("shell footer send failed for %s", self.slug)


def _split_into_code_bubbles(text: str, max_chars: int) -> list[str]:
    """Split `text` so each piece, fenced as ```\\n{piece}\\n```, fits TG.

    Prefer breaking at newlines so we don't slice in the middle of a line
    when a chunk runs long. Never returns an empty bubble.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        # A pathological single-line blob longer than max_chars: hard-slice.
        while len(line) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:max_chars])
            line = line[max_chars:]
        if len(cur) + len(line) + 1 > max_chars:
            if cur:
                out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# Bot.
# ---------------------------------------------------------------------------


# =============================================================================
# Auth providers — `/login <name>` / `/logout <name>`.
#
# Each provider knows how to:
#   - check current auth status (read-only, fast)
#   - run a login flow (typically device-code; emits progress lines via
#     a callback so the bot can post the URL/code to TG mid-flight)
#   - log out (clear local creds, optional /etc/bux/env scrub)
#
# Adding a new provider = drop a class below + register in `AUTH_PROVIDERS`.
# Keep providers framework-agnostic: no Bot reference, no TG knowledge —
# just stdout/stderr-style progress strings the caller can relay.
# =============================================================================


class _GhProvider:
    """GitHub auth via `gh auth login --web` device-code flow.

    The `--web` flag prints a one-time code + URL and polls GitHub's API
    until the user authorizes in their browser. No interactive stdin —
    we can run it as a subprocess, parse the URL/code from stderr in
    real time, send them to the user via TG, then `proc.wait()` for the
    authorization to complete (gh polls internally).

    On success we ALSO write the token into /etc/bux/env as GH_TOKEN
    so non-`gh` git operations (raw `git push https://github.com/...`)
    pick it up via the systemd EnvironmentFile mechanism.
    """

    name = "gh"
    label = "GitHub"

    def check(self) -> tuple[bool, str]:
        try:
            # Run as bux so we read /home/bux/.config/gh/hosts.yml — that's
            # where claude (which runs as bux) will look. Running as root
            # would read /root/.config/gh which claude can't see.
            r = subprocess.run(
                ["sudo", "-u", "bux", "-H", "gh", "auth", "status", "--hostname", "github.com"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # gh writes status to stderr. "Logged in to github.com account X"
            # on success; "not logged in" on failure.
            out = (r.stdout + r.stderr).strip()
            if r.returncode == 0:
                # Try to extract the account name for a friendlier message.
                for line in out.splitlines():
                    line = line.strip()
                    if "account" in line.lower() and "logged in" in line.lower():
                        return True, line
                return True, "logged in"
            return False, "not logged in"
        except FileNotFoundError:
            return False, "gh CLI not installed"
        except subprocess.TimeoutExpired:
            return False, "gh auth status timed out"

    def login(self, on_progress) -> tuple[bool, str]:
        """Run device-code flow; emit progress strings (URL + code) via
        on_progress(text) callback. Returns (success, final_status)."""
        try:
            # Run as bux (-H so HOME=/home/bux), not root. gh writes its
            # config to ~/.config/gh/hosts.yml; claude (which also runs
            # as bux) reads from the same path. If we ran as root the
            # auth would land in /root/.config and claude wouldn't see it.
            #
            # --web kicks off the device-code flow. --skip-ssh-key avoids
            # the "Generate a new SSH key?" prompt that otherwise blocks
            # even with stdin=DEVNULL on some gh versions.
            # stdin=DEVNULL: defense in depth — gh shouldn't read stdin
            # in --web mode but we make sure it can't block on it.
            proc = subprocess.Popen(
                [
                    "sudo",
                    "-u",
                    "bux",
                    "-H",
                    "gh",
                    "auth",
                    "login",
                    "--web",
                    "--hostname",
                    "github.com",
                    "--git-protocol",
                    "https",
                    "--skip-ssh-key",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # gh writes everything to stderr; merge for one stream
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            return False, "gh CLI not installed on this box"

        # Parse stdout line-by-line: gh prints
        #   "! First copy your one-time code: ABCD-1234"
        #   "Press Enter to open https://github.com/login/device in your browser..."
        # We want the code AND the URL. Send to TG as one combined message
        # so the user sees both at the same time.
        code = ""
        url = "https://github.com/login/device"
        announced = False
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = raw.rstrip()
                low = line.lower()
                if "one-time code" in low:
                    # Extract the code (last whitespace-separated token).
                    parts = line.split()
                    if parts:
                        code = parts[-1].strip()
                if "github.com/login/device" in line:
                    # Use the URL gh prints in case it ever changes.
                    for tok in line.split():
                        if tok.startswith("http"):
                            url = tok.rstrip(".")
                            break
                if not announced and code:
                    on_progress(
                        f"Open {url} on any device and enter code: {code}\n\n"
                        "I'll let you know once GitHub authorizes."
                    )
                    announced = True
                # Final success line ends the loop naturally when the pipe closes.
        except Exception:
            LOG.exception("gh login: stdout read failed")

        # `gh auth login --web` blocks until the user authorizes (or it
        # times out — gh's own ~15min default). We wait the same.
        rc = proc.wait()
        if rc != 0:
            return False, f"gh auth failed (rc={rc})"

        # Persist the token into /etc/bux/env so subsequent box-agent /
        # bux-tg restarts inherit it (gh hosts.yml lives under bux's
        # HOME and is auto-loaded by gh; GH_TOKEN env covers raw git).
        # Read the token as bux so we hit the same hosts.yml we just wrote.
        try:
            tok = subprocess.run(
                ["sudo", "-u", "bux", "-H", "gh", "auth", "token", "--hostname", "github.com"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            if tok:
                _set_box_env_var("GH_TOKEN", tok)
        except Exception:
            LOG.exception("gh login: failed to persist GH_TOKEN to /etc/bux/env")

        return True, "connected"

    def logout(self) -> tuple[bool, str]:
        try:
            # Run as bux for consistency with login/check — logs out the
            # bux user's gh, not root's.
            subprocess.run(
                ["sudo", "-u", "bux", "-H", "gh", "auth", "logout", "--hostname", "github.com"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            LOG.exception("gh logout failed")
        # Scrub GH_TOKEN regardless — even if `gh auth logout` failed,
        # the user clearly wants to be logged out.
        _unset_box_env_var("GH_TOKEN")
        return True, "logged out"


class _CodexProvider:
    """Codex auth via `codex login --device-auth`.

    The regular browser login path is awkward on headless boxes because the
    browser opens on the remote machine. Device auth prints a stable URL and
    one-time code that the Telegram bot can relay to the owner.
    """

    name = "codex"
    label = "Codex"
    DEVICE_URL = "https://auth.openai.com/codex/device"
    _CODE_RE = _DEVICE_CODE_RE

    def check(self) -> tuple[bool, str]:
        try:
            r = subprocess.run(
                ["sudo", "-iu", "bux", "codex", "login", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            out = _ANSI_RE.sub("", (r.stdout + r.stderr)).strip()
            if r.returncode == 0 and "logged in" in out.lower():
                return True, out or "logged in"
            if "command not found" in out.lower():
                return False, "codex CLI not installed"
            return False, out or "not logged in"
        except FileNotFoundError:
            return False, "codex CLI not installed"
        except subprocess.TimeoutExpired:
            return False, "codex login status timed out"

    def login(self, on_progress) -> tuple[bool, str]:
        try:
            proc = subprocess.Popen(
                ["sudo", "-iu", "bux", "codex", "login", "--device-auth"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            return False, "codex CLI not installed on this box"

        code = ""
        url = self.DEVICE_URL
        announced = False
        expect_code = False
        lines: list[str] = []
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = _ANSI_RE.sub("", raw).strip()
                if not line:
                    continue
                lines.append(line)
                for tok in line.split():
                    if tok.startswith("https://"):
                        url = tok.rstrip(".")
                        break
                low = line.lower()
                match = self._CODE_RE.search(line)
                if match and ("code" in low or "-" in match.group(0)):
                    code = match.group(0)
                elif expect_code:
                    compact = line.replace(" ", "")
                    if re.fullmatch(r"[A-Z0-9-]{6,}", compact):
                        code = compact
                expect_code = "one-time code" in low or "enter this code" in low
                if not announced and code:
                    on_progress(
                        "Open this Codex device-auth link:\n"
                        f"{url}\n\n"
                        "Enter this one-time code:\n"
                        f"`{code}`\n\n"
                        "Only enter this code on the OpenAI auth page above. "
                        "If OpenAI asks you to enable this sign-in method in "
                        "security settings, enable it there, then press "
                        "*Retry after enabling* below to restart the flow. "
                        "I'll let you know once Codex authorizes.",
                        url,
                        code,
                    )
                    announced = True
        except Exception:
            LOG.exception("codex login: stdout read failed")

        rc = proc.wait()
        if rc != 0:
            tail = "\n".join(lines[-4:]).strip()
            if tail:
                return False, f"codex auth failed (rc={rc}): {tail}"
            return False, f"codex auth failed (rc={rc})"
        return True, "connected"

    def logout(self) -> tuple[bool, str]:
        try:
            r = subprocess.run(
                ["sudo", "-iu", "bux", "codex", "logout"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode != 0:
                out = _ANSI_RE.sub("", (r.stdout + r.stderr)).strip()
                return False, out or f"codex logout failed (rc={r.returncode})"
        except Exception as e:
            LOG.exception("codex logout failed")
            return False, str(e)
        return True, "logged out"


def _set_box_env_var(key: str, value: str) -> None:
    """Append/replace KEY=VALUE in /etc/bux/env, then restart consumers
    so the new env is in their environment.

    /etc/bux/env is the EnvironmentFile= for box-agent and bux-tg. New
    values aren't picked up until the unit restarts.
    """
    # Read existing kv, replace KEY if present, otherwise append.
    existing: dict[str, str] = {}
    try:
        for line in BOX_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            existing[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    existing[key] = value
    rendered = "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n"
    # Atomic write so a concurrent reader can't see a half-file.
    tmp = BOX_ENV.with_suffix(".env.tmp")
    tmp.write_text(rendered)
    tmp.chmod(0o640)
    tmp.replace(BOX_ENV)
    # box-agent and bux-tg both EnvironmentFile this. box-agent restart
    # is async via systemctl; bux-tg restart will kill us mid-call, so
    # DON'T restart bux-tg here — the next deploy / reboot picks it up,
    # and `gh` already has the token in its own hosts.yml so the
    # new env var only matters for raw git operations.
    try:
        subprocess.run(
            ["systemctl", "restart", "box-agent.service"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        LOG.exception("failed to restart box-agent after env update")


def _unset_box_env_var(key: str) -> None:
    try:
        lines = BOX_ENV.read_text().splitlines()
    except FileNotFoundError:
        return
    kept = [
        line
        for line in lines
        if not (
            line.strip()
            and not line.strip().startswith("#")
            and line.split("=", 1)[0].strip() == key
        )
    ]
    rendered = "\n".join(kept) + ("\n" if kept else "")
    tmp = BOX_ENV.with_suffix(".env.tmp")
    tmp.write_text(rendered)
    tmp.chmod(0o640)
    tmp.replace(BOX_ENV)
    try:
        subprocess.run(
            ["systemctl", "restart", "box-agent.service"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        LOG.exception("failed to restart box-agent after env unset")


CODEX_AUTH_PROVIDER = _CodexProvider()


def _codex_login_reply_markup(url: str, code: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Open auth page", "url": url}],
            [{"text": "Copy code", "copy_text": {"text": code}}],
            [{"text": "Retry after enabling", "callback_data": "codex_login_retry"}],
        ]
    }


def _url_reply_markup(url: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Open", "url": url}],
            [{"text": "Copy link", "copy_text": {"text": url}}],
        ]
    }


def _is_codex_auth_error(text: str) -> bool:
    low = (text or "").lower()
    return (
        ("401 unauthorized" in low and "api.openai.com/v1/responses" in low)
        or ("http error: 401" in low and "responses_websocket" in low)
        or ("not logged in" in low and "codex login" in low)
    )


def _is_claude_auth_error(text: str) -> bool:
    low = (text or "").lower()
    return (
        "not logged in" in low
        or "please run" in low and "claude" in low and "login" in low
        or "claude auth login" in low
    )


def _normalize_login_provider_name(name: str) -> str:
    name = name.strip().lower()
    aliases = {
        "github": "gh",
        "git": "gh",
        "openai": "codex",
        "claude-code": "claude",
    }
    return aliases.get(name, name)


def _claude_login_status() -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["sudo", "-iu", "bux", "claude", "auth", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, "claude CLI not installed"
    except subprocess.TimeoutExpired:
        return False, "claude auth status timed out"
    out = _ANSI_RE.sub("", (r.stdout + r.stderr)).strip()
    try:
        data = json.loads(out)
    except Exception:
        data = None
    if r.returncode == 0 and isinstance(data, dict) and data.get("loggedIn") is True:
        email = data.get("email")
        method = data.get("authMethod")
        detail = "logged in"
        if email:
            detail += f" as {email}"
        if method:
            detail += f" ({method})"
        return True, detail
    if r.returncode == 0 and "logged" in out.lower():
        return True, out
    if "command not found" in out.lower():
        return False, "claude CLI not installed"
    return False, out or "not logged in"

AUTH_PROVIDERS: dict[str, object] = {
    "gh": _GhProvider(),
    "codex": CODEX_AUTH_PROVIDER,
    # Future: 'vercel': _VercelProvider(), 'npm': _NpmProvider(), etc.
}


class StreamingMessage:
    """Rolling TG message that grows in place via editMessageText, with
    every text block folded into a collapsible blockquote.

    Why blockquotes:
      Claude emits multiple text blocks per turn — narration, plans, tool
      results, and finally the answer. The first iteration of this class
      concatenated them into one growing bubble; on a chatty turn the
      user's phone showed a wall of progress and had to scroll past the
      narration to find the actual answer. TG's MarkdownV2 expandable
      blockquote (`**>...||`) is a perfect fit: collapsed by default, so
      the bubble stays compact, and tap-to-expand for users who want to
      see the trace.

    Render rules:
      * While streaming (every append): everything goes in one
        expandable blockquote so the bubble is one collapsed line.
      * On finalize() (claude's `result` event): the LAST block becomes
        the prominent answer outside the blockquote; earlier blocks stay
        collapsed underneath. Heuristic — claude almost always ends a
        turn with a real reply; if it ends on narration the wrong thing
        gets surfaced, which is fine to live with.
      * If the rendered body would exceed _MAX_BODY (TG's safe edit
        ceiling), drop the OLDEST blocks first — never the final one —
        and prepend a "…earlier messages trimmed" marker so the user knows
        content is hidden. We never multi-message: keeps the entire
        session in one editable bubble.

    Lifecycle:
      msg = StreamingMessage(bot, chat_id, thread_id)
      msg.start()         # send a `...` placeholder bubble immediately
      msg.append("…")     # one call per claude text block; edits placeholder
      msg.finalize()      # called on the `result` event; flips to final view

    Thread-safe per-instance: start() / append() / finalize() are called
    from the single parser thread; no other consumer.
    """

    # Edit at most every N seconds. TG tolerates ~1 edit/sec/chat; we
    # leave headroom and prefer "user sees the bubble grow" over one
    # huge final-burst edit at the end of a 60s turn.
    _DEBOUNCE_SEC = 1.5
    # Bound the rendered body so the single message fits comfortably
    # under TG's 4096 char hard cap on messages and edits.
    _MAX_BODY = REPLY_MAX

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        reply_to: int | None,
        thread_id: int | None,
        thinking_emoji: str | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._reply_to = reply_to  # ignored; see Bot.send docstring.
        self._thread_id = thread_id
        self._blocks: list[str] = []
        self._message_id: int | None = None
        # last rendered MDV2 we successfully sent/edited — skip an edit
        # when nothing has changed (avoids "message is not modified" 400s).
        self._last_emitted = ""
        self._last_edit_at = 0.0
        self._thinking_emoji = thinking_emoji or random_thinking_emoji()
        # Distinct parent_tool_use_ids seen for this turn → sub-agent count
        # in the header. Set so we only count each sub-agent once even
        # though it emits many events.
        self._sub_agent_ids: set[str] = set()
        # Map sub-agent tool_use_id → human description (captured from the
        # parent's Agent tool_use input) so each sub-agent's final report
        # bubble can be labeled "🤖 sub-agent: <description>".
        self._sub_agent_descriptions: dict[str, str] = {}
        # Track which sub-agent ids we've already surfaced a report for,
        # so a duplicate tool_result event doesn't double-post.
        self._sub_agent_reports_sent: set[str] = set()
        self._tool_call_count = 0
        # Sticky stats from the `result` event. The defensive finalize()
        # that runs after the stream loop has no event to read from, so
        # we cache the last-known usage/duration here and re-use them on
        # subsequent finalize() calls — otherwise the defensive call
        # would re-render WITHOUT the footer and undo it.
        self._last_usage: dict | None = None
        self._last_duration_ms: int | None = None

    def note_sub_agent(self, parent_tool_use_id: str) -> None:
        """Record a sub-agent event. If this is a new parent_tool_use_id
        the count grows and we force a rerender (debounced) so the
        `🤖 +N sub-agents` indicator appears in the header. No-op for
        ids we've already counted, so the chatty stream of sub-agent
        events doesn't beat the rate-limiter."""
        if not parent_tool_use_id or parent_tool_use_id in self._sub_agent_ids:
            return
        self._sub_agent_ids.add(parent_tool_use_id)
        self._rerender_streaming()

    def note_sub_agent_description(self, tool_use_id: str, description: str) -> None:
        """Record the human description for a sub-agent's tool_use_id so
        the eventual report bubble can be labeled. Captured from the
        parent's Agent tool_use input.description at dispatch time."""
        if tool_use_id and description:
            self._sub_agent_descriptions[tool_use_id] = description

    def send_subagent_report(self, tool_use_id: str, text: str) -> None:
        """Surface a sub-agent's final return value as its own TG bubble
        below the orchestrator bubble. Without this, the user only ever
        sees the orchestrator's synthesis — the actual sub-agent reports
        are dropped on the floor (which the orchestrator may quietly
        truncate or paraphrase). Each sub-agent posts at most once per
        turn (idempotent on tool_use_id)."""
        if not text or not tool_use_id:
            return
        if tool_use_id in self._sub_agent_reports_sent:
            return
        self._sub_agent_reports_sent.add(tool_use_id)
        desc = self._sub_agent_descriptions.get(tool_use_id, "").strip()
        header = f"🤖 sub-agent: {desc}" if desc else "🤖 sub-agent"
        body = text.strip()
        # Reserve room for header + a small MDV2 escape budget. The send
        # path is plain text (no MDV2 wrapping), so the cap is just
        # REPLY_MAX minus header overhead.
        cap = max(500, self._MAX_BODY - len(header) - 50)
        if len(body) > cap:
            body = body[:cap].rstrip() + "\n\n…(truncated)"
        msg = f"{header}\n\n{body}"
        try:
            self._bot.send(self._chat_id, msg, thread_id=self._thread_id)
        except Exception:
            LOG.exception("failed to send sub-agent report bubble")

    def note_tool_call(self, count: int = 1) -> None:
        if count <= 0:
            return
        self._tool_call_count += count

    def _rerender_streaming(self) -> None:
        """Re-emit the streaming view. Used when the header changes for
        a reason other than a new text block (e.g. a sub-agent showed
        up). Honors the debounce; the next normal append / finalize
        will catch up if we skip here."""
        if self._message_id is None or time.time() - self._last_edit_at < self._DEBOUNCE_SEC:
            return
        rendered = _render_streaming_view(
            self._blocks,
            self._MAX_BODY,
            sub_agents=len(self._sub_agent_ids),
            marker=self._thinking_emoji,
        )
        if not rendered:
            return
        self._edit(rendered)

    def start(self) -> None:
        """Send a placeholder bubble immediately, before any text has
        arrived. The emoji is a random pick from THINKING_EMOJIS so each
        turn has a slightly different vibe (approval / laughter /
        astonishment / doubt) instead of always the same 🤔.

        Sent as plain text (no MarkdownV2 wrapping) so TG mobile
        renders it as the large animated "jumboji" — wrapping the emoji
        in the expandable-blockquote markup we use for the streaming
        view downgrades it to inline-sized formatted text with no
        animation. The first `append()` / `finalize()` edits this
        message into the formatted body, which TG accepts on edit
        regardless of how the original was sent. Idempotent: no-op if
        a message has already been sent.
        """
        if self._message_id is not None:
            return
        self._message_id = self._bot.send_returning_id(
            chat_id=self._chat_id,
            text=self._thinking_emoji,
            thread_id=self._thread_id,
            markdown=False,
            pre_rendered=False,
        )
        # `_last_emitted` tracks the last RENDERED MDV2 body so we can
        # short-circuit no-op edits. We didn't send MDV2 here, so leave
        # it empty — the next _edit() won't be skipped as a no-op.
        self._last_emitted = ""
        self._last_edit_at = time.time()

    def append(self, chunk: str) -> None:
        """Record a new assistant text block; render & push (debounced)."""
        chunk = (chunk or "").strip()
        if not chunk:
            return
        self._blocks.append(chunk)
        rendered = _render_streaming_view(
            self._blocks,
            self._MAX_BODY,
            sub_agents=len(self._sub_agent_ids),
            marker=self._thinking_emoji,
        )
        if not rendered:
            return
        if self._message_id is None:
            self._send_initial(rendered)
            return
        if time.time() - self._last_edit_at < self._DEBOUNCE_SEC:
            return
        self._edit(rendered)

    def finalize(
        self,
        usage: dict | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Flip to the final view and force an edit, ignoring the debounce.

        Called once per turn on claude's `result` event. The last block
        is lifted out of the blockquote as the prominent answer; earlier
        blocks stay folded underneath. When `usage` / `duration_ms` are
        provided AND the turn actually had thinking messages, a stats footer
        is appended below the answer.

        Stats are sticky across calls: a defensive finalize() after the
        stream loop has no event to read from, but we re-use whatever
        was passed last time so the footer doesn't get stripped.
        """
        if usage is not None:
            self._last_usage = usage
        if duration_ms is not None:
            self._last_duration_ms = duration_ms
        if not self._blocks:
            return
        rendered = _render_final_view(
            self._blocks,
            self._MAX_BODY,
            sub_agents=len(self._sub_agent_ids),
            usage=self._last_usage,
            duration_ms=self._last_duration_ms,
            tool_calls=self._tool_call_count,
            marker=self._thinking_emoji,
        )
        if not rendered:
            return
        if self._message_id is None:
            self._send_initial(rendered)
            return
        self._edit(rendered)

    # ----- low-level send / edit -----

    def _send_initial(self, rendered: str) -> None:
        """First contact: send the rendered MDV2 body and stash the id."""
        self._message_id = self._bot.send_returning_id(
            chat_id=self._chat_id,
            text=rendered,
            thread_id=self._thread_id,
            pre_rendered=True,
        )
        self._last_emitted = rendered
        self._last_edit_at = time.time()

    def _edit(self, rendered: str) -> None:
        """Push `rendered` to the open message. Skips no-op edits."""
        if self._message_id is None or rendered == self._last_emitted:
            return
        ok = self._bot.edit(
            chat_id=self._chat_id,
            message_id=self._message_id,
            text=rendered,
            pre_rendered=True,
        )
        if ok:
            self._last_emitted = rendered
            self._last_edit_at = time.time()


class Bot:
    def __init__(self, token: str, setup_token: str) -> None:
        self.token = token
        self.setup_token = setup_token
        self.api = f"https://api.telegram.org/bot{token}"
        self.client = httpx.Client(timeout=POLL_TIMEOUT + 10)
        self.state = load_state()

    # ----- Telegram API plumbing -----

    def call(self, method: str, **params) -> dict:
        # Auto-suppress link previews on every message we send or edit.
        # PR comments, docs URLs, trace links etc. would otherwise eat a
        # huge chunk of phone-screen real estate with their preview cards.
        # Callers can still override by passing link_preview_options
        # explicitly (e.g. {"is_disabled": False}).
        if method in ("sendMessage", "editMessageText"):
            params.setdefault("link_preview_options", {"is_disabled": True})
        try:
            r = self.client.post(
                f"{self.api}/{method}",
                json={k: v for k, v in params.items() if v is not None},
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            body = ""
            try:
                body = e.response.text
            except Exception:
                pass
            LOG.warning("%s failed: %s body=%s", method, e, body[:300])
            return {"ok": False, "error_code": e.response.status_code, "description": body}
        except Exception as e:
            LOG.warning("%s failed: %s", method, e)
            return {"ok": False}

    def send(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        thread_id: int | None = None,
        markdown: bool = False,
        pre_rendered: bool = False,
        reply_markup: dict | None = None,
    ) -> None:
        """Send a message into a chat (and optional forum topic).

        If `markdown=True`, runs the input through MarkdownV2 conversion and
        retries as plain text on the inevitable 400 (TG's escape rules are a
        minefield). Plain text path is the default for bot-authored strings
        so a stray `_` doesn't trip on its way out.

        `thread_id` routes the reply into a forum topic so a per-topic
        conversation stays scoped.

        `reply_to` is accepted for backward-compat with existing call sites
        but ignored: phone-screen UX is cleaner without TG's quoted-reply
        affordance on every bot message. A follow-up can rip the parameter
        out across the file.

        `pre_rendered=True` means `text` is already MarkdownV2 — skip the
        converter but still send with parse_mode=MarkdownV2. Used by the
        streaming renderer which composes its own blockquote markup.
        """
        self.send_returning_id(
            chat_id=chat_id, text=text, reply_to=reply_to,
            thread_id=thread_id, markdown=markdown, pre_rendered=pre_rendered,
            reply_markup=reply_markup,
        )

    def send_returning_id(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        thread_id: int | None = None,
        markdown: bool = False,
        pre_rendered: bool = False,
        reply_markup: dict | None = None,
    ) -> int | None:
        """Same as send() but returns the message_id of the FIRST chunk.

        Used by streaming flows that want to edit the message in place as
        more text arrives — they need the id to call editMessageText.
        For multi-chunk sends, only the first id is returned (subsequent
        chunks get their own ids that the caller can't address).

        `reply_to` is ignored — see `send()` for the rationale.
        `pre_rendered=True` skips MDV2 conversion (text is already MDV2).
        """
        del reply_to
        as_markdown = markdown or pre_rendered
        chunks = (
            _chunk_for_telegram(text, REPLY_MAX)
            if as_markdown
            else [text[i : i + REPLY_MAX] or " " for i in range(0, max(len(text), 1), REPLY_MAX)]
        )
        first_id: int | None = None
        for chunk in chunks:
            if as_markdown:
                rendered = chunk if pre_rendered else _to_tg_markdown_v2(chunk)
                resp = self.call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=rendered,
                    message_thread_id=thread_id or None,
                    parse_mode="MarkdownV2",
                    reply_markup=reply_markup,
                )
                if resp.get("ok") is False and resp.get("error_code") == 400:
                    LOG.info("MarkdownV2 rejected, falling back to plain text")
                    resp = self.call(
                        "sendMessage",
                        chat_id=chat_id,
                        text=chunk,
                        message_thread_id=thread_id or None,
                        reply_markup=reply_markup,
                    )
                    if resp.get("ok") is False and reply_markup:
                        LOG.info("plain sendMessage with reply_markup rejected, retrying without markup")
                        resp = self.call(
                            "sendMessage",
                            chat_id=chat_id,
                            text=chunk,
                            message_thread_id=thread_id or None,
                        )
            else:
                resp = self.call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=chunk,
                    message_thread_id=thread_id or None,
                    reply_markup=reply_markup,
                )
                if resp.get("ok") is False and reply_markup:
                    LOG.info("sendMessage with reply_markup rejected, retrying without markup")
                    resp = self.call(
                        "sendMessage",
                        chat_id=chat_id,
                        text=chunk,
                        message_thread_id=thread_id or None,
                    )
            if first_id is None and resp.get("ok"):
                mid = (resp.get("result") or {}).get("message_id")
                if isinstance(mid, int):
                    first_id = mid
        return first_id

    def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        markdown: bool = False,
        pre_rendered: bool = False,
    ) -> bool:
        """Edit a previously-sent message in place. Returns True on success.

        Handles three TG edge cases:
          * 400 "message is not modified" — text was identical; treat as
            success since the on-screen content already matches.
          * 400 (other) on MarkdownV2 — escape mistake; retry as plain text.
          * 429 — rate-limited; sleep retry_after (capped) and retry once.
            The streaming caller debounces at 1.5s, so 429s are rare
            unless there are many concurrent chats; the single retry
            covers transient bursts without blocking forever.

        `pre_rendered=True` means `text` is already MarkdownV2 — skip the
        converter but still send with parse_mode=MarkdownV2.
        """
        as_markdown = markdown or pre_rendered

        def _do_edit() -> dict:
            if as_markdown:
                rendered = text if pre_rendered else _to_tg_markdown_v2(text)
                resp = self.call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=message_id,
                    text=rendered,
                    parse_mode="MarkdownV2",
                )
                if resp.get("ok") is False and resp.get("error_code") == 400:
                    # Could be "not modified" OR a MarkdownV2 escape error.
                    # On the first kind we want to claim success. On the
                    # second we want to retry as plain text. The body's
                    # description tells us which.
                    desc = (resp.get("description") or "").lower()
                    if "not modified" in desc:
                        return {"ok": True, "_skipped": True}
                    return self.call(
                        "editMessageText",
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                    )
                return resp
            resp = self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
            if resp.get("ok") is False and resp.get("error_code") == 400:
                desc = (resp.get("description") or "").lower()
                if "not modified" in desc:
                    return {"ok": True, "_skipped": True}
            return resp

        resp = _do_edit()
        if resp.get("ok") is False and resp.get("error_code") == 429:
            # TG returns retry_after in the JSON description for 429s.
            # Parse defensively — we never want a parse error here to
            # raise into the streaming loop.
            retry_after = 1
            try:
                desc = resp.get("description") or ""
                # Body looks like '{"ok":false,"error_code":429,...,"parameters":{"retry_after":3}}'
                parsed = json.loads(desc) if desc.startswith("{") else {}
                ra = (parsed.get("parameters") or {}).get("retry_after")
                if isinstance(ra, (int, float)) and ra > 0:
                    retry_after = min(int(ra), 5)
            except Exception:
                pass
            time.sleep(retry_after)
            resp = _do_edit()
        return bool(resp.get("ok"))

    def typing(self, chat_id: int, thread_id: int | None = None) -> None:
        self.call(
            "sendChatAction",
            chat_id=chat_id,
            action="typing",
            message_thread_id=thread_id or None,
        )

    def react(self, chat_id: int, message_id: int | None, emoji: str | None) -> None:
        """Set a single-emoji reaction on the user's message; emoji=None clears.

        Telegram's free-account allowlist excludes ⏳/✅/⚠️/❌. Failures are
        swallowed so a rejected emoji never poisons the running task.
        """
        if not message_id:
            return
        reaction = [] if emoji is None else [{"type": "emoji", "emoji": emoji}]
        self.call(
            "setMessageReaction",
            chat_id=chat_id,
            message_id=message_id,
            reaction=reaction,
        )

    # ----- Agent dispatch -----

    def _build_env(
        self,
        key: LaneKey,
        agent: str,
        sender: dict | None = None,
    ) -> dict[str, str]:
        """Forwarded env passed to the agent subprocess via `sudo VAR=val …`.

        Returns ONLY the keys we explicitly want the agent to see — never
        the bot's own env. This matters: the bot inherits TG_BOT_TOKEN /
        TG_SETUP_TOKEN from systemd, and we don't want those reaching
        claude/codex (and from there, every tool invocation).

        Picks up:
          - HOME / USER / PATH so login-shell paths resolve (needed for
            the per-user `~/.npm-global/bin/codex` lookup, ~/.local/bin tools).
          - BU_* + BROWSER_USE_API_KEY + BUX_PROFILE_ID so claude can drive
            the browser without re-fetching credentials.
          - OPENAI_* from /home/bux/.secrets/openai.env (codex needs this).
          - TG_CHAT_ID + TG_THREAD_ID so `tg-send` routes back to this
            forum topic when the agent shells out for background work.
          - TG_USER_ID / TG_USERNAME / TG_FROM_NAME so the agent (and any
            tool it shells out to) can attribute the current turn to a
            specific human in a multi-user group chat.
          - TG_OWNER_ID / TG_OWNER_NAME / TG_IS_OWNER for the soft "first
            binder is owner, others are guests" model. Authorization stays
            chat-scoped — these are just attribution hints.
        """
        box_env = _read_kv(BOX_ENV)
        browser_env = _read_kv(BROWSER_ENV)
        openai_env = _read_kv(OPENAI_ENV)

        env: dict[str, str] = {
            "HOME": "/home/bux",
            "USER": "bux",
            # Match a typical bux login shell: per-user installs shadow system ones,
            # then standard system bins. The bot's own PATH (root) isn't relevant.
            "PATH": "/home/bux/.local/bin:/home/bux/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
        }
        if box_env.get("BROWSER_USE_API_KEY"):
            env["BROWSER_USE_API_KEY"] = box_env["BROWSER_USE_API_KEY"]
        if box_env.get("BUX_PROFILE_ID"):
            env["BUX_PROFILE_ID"] = box_env["BUX_PROFILE_ID"]
            env["BU_PROFILE_ID"] = box_env["BUX_PROFILE_ID"]
        for k in ("BU_CDP_WS", "BU_BROWSER_ID"):
            if browser_env.get(k):
                env[k] = browser_env[k]
        for k, v in openai_env.items():
            if k.startswith("OPENAI_"):
                env[k] = v
        # Routing back to this topic from any tool the agent shells out to.
        chat, thread = key
        env["TG_CHAT_ID"] = str(chat)
        if thread:
            env["TG_THREAD_ID"] = str(thread)
        if sender:
            if sender.get("user_id"):
                env["TG_USER_ID"] = sender["user_id"]
            if sender.get("username"):
                env["TG_USERNAME"] = sender["username"]
            if sender.get("name"):
                env["TG_FROM_NAME"] = sender["name"]
        owner = _owner_for(chat, self.state)
        if owner:
            if owner.get("user_id"):
                env["TG_OWNER_ID"] = owner["user_id"]
            if owner.get("name"):
                env["TG_OWNER_NAME"] = owner["name"]
            env["TG_IS_OWNER"] = "true" if _is_owner(sender, owner) else "false"
        # Strip newlines from forwarded values: argv accepts them but downstream
        # tooling (`tg-send`, bash one-liners) tends to break on embedded \n.
        # Tabs are similarly unsafe in `KEY=val` argv positions.
        return {
            k: v.replace("\n", " ").replace("\r", " ").replace("\t", " ") for k, v in env.items()
        }

    def run_task(
        self,
        key: LaneKey,
        prompt: str,
        reply_to: int | None,
        sender: dict | None = None,
        thinking_emoji: str | None = None,
    ) -> None:
        """Dispatch the user's prompt to this lane's agent.

        Streams events from the agent's stdout straight to TG as they arrive
        so the user sees progress instead of one big wall of text at the end.
        Returns when the agent's `result` (or `turn.completed`/`turn.failed`)
        event fires — the lane worker's semaphore is then released and the
        next queued message in the lane can run.

        For tasks that should outlive this call (>60s research, multi-step
        flows), the agent is expected to background them via bash:
            nohup bash -c '... | tg-send' &
        so this run_task returns quickly and the lane unblocks. See CLAUDE.md
        section "Long tasks → background a worker" for the pattern.
        """
        chat_id, thread_id = key
        agent = _agent_for(key, self.state)
        LOG.info("run_task lane=%s agent=%s", _lane_slug(key), agent)
        try:
            if agent == AGENT_CODEX:
                self._run_codex(key, prompt, reply_to, sender=sender, thinking_emoji=thinking_emoji)
            else:
                self._run_claude(key, prompt, reply_to, sender=sender, thinking_emoji=thinking_emoji)
        except Exception as e:
            LOG.exception("run_task lane=%s failed", _lane_slug(key))
            try:
                self.react(chat_id, reply_to, EMOJI_ERROR)
                self.send(
                    chat_id,
                    f"❌ task failed: {e}",
                    reply_to=reply_to,
                    thread_id=thread_id,
                )
            except Exception:
                LOG.exception("also failed to send error reply")

    def _run_claude(
        self,
        key: LaneKey,
        prompt: str,
        reply_to: int | None,
        sender: dict | None = None,
        thinking_emoji: str | None = None,
    ) -> None:
        """Stream a claude turn into the lane's TG topic.

        Uses --output-format=stream-json so each parent assistant text
        block lands as its own TG message bubble as it arrives. Sub-agent
        internal events (those carrying parent_tool_use_id) are silently
        dropped — the user only sees the orchestrator's voice.
        """
        chat_id, thread_id = key
        env = self._build_env(key, AGENT_CLAUDE, sender=sender)
        sid = _session_uuid_for(key)
        prompt = _prefix_sender(prompt, sender, _owner_for(chat_id, self.state))

        # Claude Code 2.x split create vs. resume: `--session-id <uuid>`
        # creates and errors if the UUID already exists, `--resume <uuid>`
        # continues an existing session. Pick based on transcript presence.
        cmd = ["sudo", "-u", "bux", "-H"] + [f"{k}={v}" for k, v in env.items() if v]
        cmd += [
            "/usr/bin/claude",
            "-p",
            *_claude_session_flag(sid),
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "stream-json",
            "--verbose",  # stream-json requires --verbose
            prompt,
        ]

        try:
            # stderr=DEVNULL: stream-json puts everything we care about on
            # stdout; capturing stderr unread risks a 64 KB pipe deadlock
            # on a chatty claude. The fallback path captures both for the
            # no-output case.
            # stdin=DEVNULL: claude itself doesn't read stdin under -p, but
            # children it shells out to (gh auth login, ssh, vim, psql) do,
            # and a child blocking on stdin would wedge the whole lane.
            # /dev/null hands them an immediate EOF so they fail loudly.
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                cwd=str(WORKSPACE),
            )
        except Exception as e:
            self.react(chat_id, reply_to, EMOJI_ERROR)
            self.send(
                chat_id,
                f"❌ failed to spawn claude: {e}",
                reply_to=reply_to,
                thread_id=thread_id,
            )
            return

        # Register the proc so `/cancel` (per lane) can SIGKILL it when the
        # agent is wedged on an interactive child. Removed in `finally`.
        slug = _lane_slug(key)
        with _inflight_lock:
            _inflight_procs[slug] = proc

        # No wall-clock cap: the agent can run as long as the user's task
        # needs. A genuinely-stuck claude pid sits forever until the user
        # kills it from ssh; that cost is local to one topic.

        # One rolling TG message per turn — every assistant text block
        # appends + edits, instead of N separate sendMessage calls. User
        # gets one notification for the whole turn. See StreamingMessage
        # docstring for the why. start() sends a `...` placeholder bubble
        # immediately so the user has a visible "I'm on it" signal, even
        # before claude has emitted its first text block.
        stream_msg = StreamingMessage(self, chat_id, reply_to, thread_id, thinking_emoji=thinking_emoji)
        stream_msg.start()
        any_text = False
        assert proc.stdout is not None
        try:
            try:
                for line in proc.stdout:
                    try:
                        ev = json.loads(line.strip() or "{}")
                    except Exception:
                        continue
                    et = ev.get("type")
                    parent_id = ev.get("parent_tool_use_id")
                    tool_uses = _count_claude_tool_uses(ev)
                    if tool_uses:
                        stream_msg.note_tool_call(tool_uses)
                    # Sub-agent internals don't show their text in the
                    # bubble, but we count distinct parent_tool_use_ids so
                    # the header can show `🤖 +N sub-agents`. Anything
                    # beyond `assistant` carrying that id (tool_result,
                    # user, system) still represents the sub-agent doing
                    # something, so we count those too.
                    if parent_id:
                        stream_msg.note_sub_agent(parent_id)
                        continue
                    if et == "assistant":
                        content = (ev.get("message") or {}).get("content") or []
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            # Capture descriptions for Agent tool_use calls so
                            # the sub-agent's eventual report bubble can be
                            # labeled. Other tool_use blocks pass through.
                            if btype == "tool_use":
                                tname = (block.get("name") or "").lower()
                                if tname in {"task", "agent"}:
                                    tu_id = block.get("id") or ""
                                    tinput = block.get("input") if isinstance(block.get("input"), dict) else {}
                                    desc = (tinput.get("description")
                                            or tinput.get("subagent_type")
                                            or "")
                                    stream_msg.note_sub_agent_description(tu_id, str(desc))
                                continue
                            if btype != "text":
                                continue
                            text = (block.get("text") or "").strip()
                            if not text:
                                continue
                            stream_msg.append(text)
                            if not any_text:
                                any_text = True
                    elif et == "user":
                        # tool_results coming back to the parent. For
                        # results matching a sub-agent we've seen, surface
                        # the report as its own bubble — otherwise the
                        # user never sees the sub-agent's actual findings.
                        msg = ev.get("message") or {}
                        ucontent = msg.get("content") or []
                        if isinstance(ucontent, list):
                            for block in ucontent:
                                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                                    continue
                                tu_id = block.get("tool_use_id") or ""
                                if not tu_id or tu_id not in stream_msg._sub_agent_ids:
                                    continue
                                tr_content = block.get("content")
                                report_text = ""
                                if isinstance(tr_content, str):
                                    report_text = tr_content
                                elif isinstance(tr_content, list):
                                    parts = []
                                    for b in tr_content:
                                        if isinstance(b, dict) and b.get("type") == "text":
                                            t = b.get("text") or ""
                                            if t:
                                                parts.append(t)
                                    report_text = "\n\n".join(parts)
                                if report_text:
                                    stream_msg.send_subagent_report(tu_id, report_text)
                    elif et == "result":
                        # Turn complete; flip to the final view (last block
                        # prominent, earlier blocks collapsed underneath).
                        # Pull token usage + duration from the result so
                        # the footer can show token / tool / duration stats.
                        usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else None
                        duration_ms = ev.get("duration_ms")
                        if not isinstance(duration_ms, int):
                            duration_ms = None
                        stream_msg.finalize(usage=usage, duration_ms=duration_ms)
                        break
            except Exception:
                LOG.exception("stream-json loop failed; falling back to plain run")
            # Defensive: render whatever's pending even on early exit /
            # parse errors, so a user mid-turn doesn't see a half-truncated
            # bubble. finalize() is idempotent and force-edits past the
            # debounce, which is what we want here.
            stream_msg.finalize()

            # Drain the rest, kill the proc if it's lingering (tool wait, etc.).
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

            # SIGKILL/SIGTERM means /cancel (or the timeout-kill above) ended
            # the proc. Reply "🛑 Cancelled." into the lane and SKIP the
            # no-output fallback — otherwise we'd silently re-run the same
            # prompt the user just cancelled. A clean exit (rc >= 0) falls
            # through to the normal fallback path below.
            if proc.returncode in (-9, -15):
                self.react(chat_id, reply_to, EMOJI_ERROR)
                self.send(
                    chat_id,
                    "🛑 Cancelled.",
                    reply_to=reply_to,
                    thread_id=thread_id,
                )
                return

            if not any_text:
                # Stream produced nothing visible — fall back to a plain run so
                # the user gets *something*. Keeps the bot honest if claude
                # hiccuped on the streaming format.
                fb_cmd = ["sudo", "-u", "bux", "-H"] + [f"{k}={v}" for k, v in env.items() if v]
                fb_cmd += [
                    "/usr/bin/claude",
                    "-p",
                    *_claude_session_flag(sid),
                    "--permission-mode",
                    "bypassPermissions",
                    "--output-format",
                    "text",
                    prompt,
                ]
                try:
                    # No env= here: the `sudo VAR=val …` prefix is the only env
                    # the child claude sees. Don't leak the bot's own environ
                    # (TG_BOT_TOKEN, TG_SETUP_TOKEN) through sudo.
                    fb = subprocess.run(
                        fb_cmd,
                        capture_output=True,
                        text=True,
                        timeout=1800,
                        cwd=str(WORKSPACE),
                    )
                    out = (fb.stdout or "").strip()
                    if not out:
                        out = (fb.stderr or "").strip() or f"(no output; rc={fb.returncode})"
                    if _is_claude_auth_error(out):
                        self.send(
                            chat_id,
                            "Claude is not logged in. Starting `/login claude` now.",
                            reply_to=reply_to,
                            thread_id=thread_id,
                            markdown=True,
                        )
                        self._cmd_claude_login(
                            chat_id,
                            reply_to,
                            thread_id,
                            slug,
                            sender or {},
                            _owner_for(chat_id, self.state),
                        )
                        return
                    self.send(
                        chat_id,
                        out,
                        reply_to=reply_to,
                        thread_id=thread_id,
                        markdown=True,
                    )
                    if fb.returncode != 0:
                        self.react(chat_id, reply_to, EMOJI_ERROR)
                except subprocess.TimeoutExpired:
                    self.react(chat_id, reply_to, EMOJI_ERROR)
                    self.send(
                        chat_id,
                        "⏱ Timed out after 30 min.",
                        reply_to=reply_to,
                        thread_id=thread_id,
                    )
                except Exception as e:
                    self.react(chat_id, reply_to, EMOJI_ERROR)
                    self.send(
                        chat_id,
                        f"❌ task failed: {e}",
                        reply_to=reply_to,
                        thread_id=thread_id,
                    )
        finally:
            # Always release the inflight handle for this lane, even on early
            # error returns above — `slug` was set right after Popen succeeded.
            with _inflight_lock:
                if _inflight_procs.get(slug) is proc:
                    _inflight_procs.pop(slug, None)

    def _run_codex(
        self,
        key: LaneKey,
        prompt: str,
        reply_to: int | None,
        sender: dict | None = None,
        thinking_emoji: str | None = None,
    ) -> None:
        """Stream a codex turn into the lane's TG topic.

        Uses `codex exec --json` (JSONL events). Forwards `item.completed`
        events of type `agent_message` as TG bubbles.

        Per-lane continuity: the first message in a lane runs `codex exec`,
        captures `thread_id` from the `thread.started` event, and persists
        it to disk. Subsequent messages in the same lane run
        `codex exec resume <thread_id>` so conversation context survives
        across messages — same shape as claude's `--session-id` model.
        """
        chat_id, thread_id = key
        env = self._build_env(key, AGENT_CODEX, sender=sender)
        existing_thread = _codex_thread_id_for(key)
        prompt = _prefix_sender(prompt, sender, _owner_for(chat_id, self.state))

        # Refuse early if codex isn't installed — the user gets a clear
        # install hint instead of a confusing FileNotFoundError.
        codex_bin = self._which_codex()
        if not codex_bin:
            self.react(chat_id, reply_to, EMOJI_ERROR)
            self.send(
                chat_id,
                "❌ codex CLI not installed on this box. Install with `npm install -g "
                "@openai/codex`, then sign in either way:\n"
                "• ChatGPT subscription: send `/codex login` here, or run "
                "`codex login --device-auth` as the bux user from a terminal.\n"
                "• API key: drop `OPENAI_API_KEY=...` into `/home/bux/.secrets/openai.env`.\n\n"
                "Pick one — if both are set, codex silently uses the API key "
                "for billing (openai/codex#20099).\n\n"
                "Or `/claude` to switch back.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return

        # No OPENAI_API_KEY pre-flight check: codex also auths via `codex login`
        # (ChatGPT subscription, creds at ~bux/.codex/auth.json) and we don't
        # want to error out for users on that path. If neither auth is set up,
        # codex itself emits a clear stderr message which we surface below.

        cmd = ["sudo", "-u", "bux", "-H"] + [f"{k}={v}" for k, v in env.items() if v]
        # `-a never -s danger-full-access` is codex's equivalent of claude's
        # `--dangerously-skip-permissions`: never escalate to a human for
        # approval, and let the model run shell commands with full disk
        # access. Required for the bot's non-interactive lane model — there's
        # nobody attached to answer an approval prompt, so without these
        # codex stalls forever on any non-trivial command.
        # Both flags are top-level (codex), not subcommand-level (exec).
        codex_bypass_flags = [
            "-a", "never",
            "-s", "danger-full-access",
        ]
        if existing_thread:
            # Resume the lane's existing codex thread so conversation context
            # carries across messages. `codex exec resume <id>` is the
            # documented form. If the thread id is invalid (codex pruned it,
            # disk corruption), codex errors and we surface the stderr.
            cmd += [
                codex_bin,
                *codex_bypass_flags,
                "exec",
                "resume",
                existing_thread,
                "--json",
                "--skip-git-repo-check",
                prompt,
            ]
        else:
            # First message in this lane — `thread.started` arrives in the
            # JSONL stream and we persist its id below.
            cmd += [
                codex_bin,
                *codex_bypass_flags,
                "exec",
                "--json",
                "--skip-git-repo-check",
                prompt,
            ]

        # stderr → file rather than DEVNULL so the no-output path can
        # surface the actual error message to the user (codex tends to
        # print friendly errors to stderr on auth / rate-limit issues).
        # Using a tempfile (not PIPE) avoids the 64 KB pipe deadlock.
        import tempfile

        stderr_buf = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            # stdin=DEVNULL: codex doesn't read stdin in `exec --json` mode,
            # but children it spawns (gh, ssh, vim, psql) can — and one such
            # child blocking on stdin would wedge the entire lane. /dev/null
            # hands them an immediate EOF so they fail loudly.
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_buf,
                text=True,
                cwd=str(WORKSPACE),
            )
        except Exception as e:
            # Popen never produced a child — close the stderr tempfile
            # ourselves; nothing else will. Without this the FD leaks per
            # failed spawn (rare, but a misconfigured PATH or OOM at fork
            # time would silently drain the bot's FD table over time).
            try:
                stderr_buf.close()
            except Exception:
                pass
            self.react(chat_id, reply_to, EMOJI_ERROR)
            self.send(
                chat_id,
                f"❌ failed to spawn codex: {e}",
                reply_to=reply_to,
                thread_id=thread_id,
            )
            return

        # Register the proc so `/cancel` (per lane) can SIGKILL it. Removed
        # in `finally` so an early-error path can't strand a stale handle.
        slug = _lane_slug(key)
        with _inflight_lock:
            _inflight_procs[slug] = proc

        # Match the claude path: one rolling TG bubble per turn, started
        # immediately with a placeholder + random thinking emoji so the
        # user has visible "I'm on it" feedback before the first text.
        # Codex usually emits a single `agent_message` per turn, but
        # multi-message turns work too — they just stream as additional
        # blocks into the same bubble.
        stream_msg = StreamingMessage(self, chat_id, reply_to, thread_id, thinking_emoji=thinking_emoji)
        stream_msg.start()
        started_at = time.time()
        any_text = False
        assert proc.stdout is not None
        try:
            try:
                for line in proc.stdout:
                    try:
                        ev = json.loads(line.strip() or "{}")
                    except Exception:
                        continue
                    et = ev.get("type") or ""
                    # Codex JSONL events of interest:
                    #   thread.started { thread_id: '...' }       — first turn only; persist for resume
                    #   item.completed { item: { type: 'agent_message', text: '...' } }
                    #   turn.completed { usage: { input_tokens, cached_input_tokens, output_tokens } }
                    #   turn.failed (terminal, error)
                    if et == "thread.started" and not existing_thread:
                        # Field shape per codex docs: top-level `thread_id`. Be
                        # defensive against schema drift — also check nested
                        # `thread.id` / `session.id` shapes a future codex
                        # version might emit.
                        new_tid = (
                            ev.get("thread_id")
                            or (ev.get("thread") or {}).get("id")
                            or (ev.get("session") or {}).get("id")
                        )
                        if new_tid:
                            try:
                                _save_codex_thread_id(key, str(new_tid))
                                existing_thread = str(new_tid)
                            except Exception:
                                LOG.exception("persist codex thread_id failed")
                    elif et.startswith("item.") and et.endswith("completed"):
                        item = ev.get("item") or {}
                        if _is_codex_tool_event(ev):
                            stream_msg.note_tool_call()
                        if item.get("type") == "agent_message":
                            text = (item.get("text") or "").strip()
                            if text:
                                stream_msg.append(text)
                                if not any_text:
                                    any_text = True
                    elif et in ("turn.completed", "turn.failed"):
                        # `turn.failed` is the only true terminal-error signal.
                        # `error` events also appear during transient reconnects
                        # ("Reconnecting... 1/5") and aren't fatal — see below.
                        usage = _normalize_codex_usage(ev.get("usage"))
                        duration_ms = int((time.time() - started_at) * 1000)
                        stream_msg.finalize(usage=usage, duration_ms=duration_ms)
                        break
                    elif et == "error":
                        # Don't terminate the loop here: codex emits transient
                        # `error` notices (reconnection retries, rate-limit
                        # backoff) as progress signals. Just log and keep
                        # streaming; if the failure is real, `turn.failed` will
                        # arrive next and break us out.
                        LOG.warning("codex transient error: %s", ev.get("message") or ev)
            except Exception:
                LOG.exception("codex JSONL loop failed")
            # Defensive: same idempotent finalize as the claude path so a
            # stream that breaks mid-turn still flips the bubble out of the
            # placeholder state instead of leaving a dangling "..." view.
            stream_msg.finalize()

            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

            # SIGKILL/SIGTERM means /cancel ended the proc. Reply
            # "🛑 Cancelled." into the lane and SKIP the no-output fallback
            # so we don't surface a confusing stderr message for a kill the
            # user explicitly asked for. A clean exit (rc >= 0) falls
            # through to the normal no-output handling below.
            if proc.returncode in (-9, -15):
                self.react(chat_id, reply_to, EMOJI_ERROR)
                self.send(
                    chat_id,
                    "🛑 Cancelled.",
                    reply_to=reply_to,
                    thread_id=thread_id,
                )
                return

            if not any_text:
                err = ""
                try:
                    stderr_buf.seek(0)
                    err = stderr_buf.read().strip()
                except Exception:
                    pass
                self.react(chat_id, reply_to, EMOJI_ERROR)
                if _is_codex_auth_error(err):
                    self.send(
                        chat_id,
                        "Codex auth failed with a 401. Starting `/codex login` now.",
                        reply_to=reply_to,
                        thread_id=thread_id,
                        markdown=True,
                    )
                    self._start_login_provider(
                        "codex",
                        CODEX_AUTH_PROVIDER,
                        chat_id,
                        reply_to,
                        thread_id,
                        force=True,
                    )
                    return
                self.send(
                    chat_id,
                    err or "(codex returned no output)",
                    reply_to=reply_to,
                    thread_id=thread_id,
                )
        finally:
            with _inflight_lock:
                if _inflight_procs.get(slug) is proc:
                    _inflight_procs.pop(slug, None)
            try:
                stderr_buf.close()
            except Exception:
                pass

    @staticmethod
    def _which_codex() -> str | None:
        for p in (
            "/usr/local/bin/codex",
            "/usr/bin/codex",
            "/home/bux/.npm-global/bin/codex",
        ):
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return None

    # ----- Binding flow -----

    def assert_my_commands(self) -> None:
        """Register the bot's slash commands with Telegram so typing `/`
        in chat surfaces the autocomplete tooltip. Run on every startup
        so the registered list stays in sync with the code.

        Descriptions are short — Telegram clips them in the popup. Order
        is roughly "most useful first"; TG renders them as listed.
        """
        commands = [
            {"command": name, "description": desc}
            for name, desc in BOT_COMMANDS
        ]
        res = self.call("setMyCommands", commands=commands)
        if res.get("ok"):
            LOG.info("registered %d slash commands with Telegram", len(commands))
        else:
            LOG.warning("setMyCommands failed: %s", res)

    def assert_default_admin_rights(self) -> None:
        """Assert the default admin rights for `?startgroup=…` deeplinks.

        Run on every startup so the BotFather-side default stays in sync with
        code. With these rights pre-prompted at install, the bot ends up as
        admin in any group it's added to, which bypasses the BotFather privacy
        toggle (no `/setprivacy → Disable` manual step) and lets the bot
        manage forum topics + pin its own welcome.

        Minimum-viable rights only — no member moderation, no chat-info edit,
        no member promotion. Telegram requires `can_manage_chat: true` for the
        actor to be considered an admin at all.
        """
        rights = {
            "is_anonymous": False,
            "can_manage_chat": True,
            "can_delete_messages": False,
            "can_manage_video_chats": False,
            "can_restrict_members": False,
            "can_promote_members": False,
            "can_change_info": False,
            "can_invite_users": False,
            "can_pin_messages": True,
            "can_manage_topics": True,
        }
        res = self.call(
            "setMyDefaultAdministratorRights",
            rights=rights,
            for_channels=False,
        )
        if res.get("ok"):
            granted = ",".join(k for k, v in rights.items() if v is True)
            LOG.info("default admin rights asserted: %s", granted)
        else:
            LOG.warning("setMyDefaultAdministratorRights failed: %s", res)

    def _bind_chat(self, chat_id: int, sender: dict | None = None) -> None:
        """Register chat_id, burn the setup_token, record the owner, welcome."""
        add_allow(chat_id)
        burn_setup_token()
        self.setup_token = ""
        if sender and sender.get("user_id"):
            _set_owner_for(chat_id, sender, self.state)
            _set_box_owner(self.state, sender)
            LOG.info(
                "authorized chat_id=%s owner=%s (id=%s)",
                chat_id,
                sender.get("name") or sender.get("username") or "?",
                sender.get("user_id"),
            )
        else:
            LOG.info("authorized chat_id=%s (no sender info)", chat_id)
        self.send(
            chat_id,
            "✓ Linked.\n\n"
            f"Chat id: {chat_id}\n\n"
            "🔒 This bot is now locked to this chat only. Every other chat is "
            "silently dropped — even if someone discovers the bot handle.\n\n"
            "Text me anything and I'll run it on your bux. Want parallel work? "
            "Turn on Topics in this chat and each topic becomes a separate "
            "agent session. Use `/codex` per-topic to switch from claude.",
        )

    def _auto_allow_chat(
        self,
        chat_id: int,
        sender: dict | None,
        announce: bool = True,
    ) -> None:
        """Allow-list a new chat that the box owner just added the bot to.

        Mirror of `_bind_chat` minus the setup-token burn (already burned
        on first bind). Idempotent: re-running on an already-allowed chat
        skips the welcome but still backfills the per-chat owner record.
        """
        already = chat_id in load_allow()
        if not already:
            add_allow(chat_id)
        if sender and sender.get("user_id"):
            _set_owner_for(chat_id, sender, self.state)
        if already:
            return
        if announce:
            try:
                self.send(
                    chat_id,
                    "✓ Activated for this chat (you're the box owner).\n\n"
                    "Topics inside are auto-allowed. Text me anything.",
                )
            except Exception:
                LOG.exception("auto-allow welcome send failed for chat_id=%s", chat_id)

    def _handle_my_chat_member(self, update: dict) -> None:
        """React to the bot's own membership changing in some chat.

        Telegram fires `my_chat_member` whenever the bot is added to a
        chat, removed, or has its admin rights changed. We use it to
        auto-allow chats that the box owner adds the bot to — the
        primary path for "I just made a new group, let me use it".

        Security: only `from.id == box_owner.user_id` triggers auto-allow.
        Anyone else adding the bot is silently ignored (the chat stays
        denied at the message gate).
        """
        try:
            chat = update.get("chat") or {}
            chat_id = chat.get("id")
            if not chat_id:
                return
            actor_raw = update.get("from") or {}
            actor = _extract_sender({"from": actor_raw})
            box_owner = _box_owner(self.state)
            if not box_owner:
                LOG.info("my_chat_member chat_id=%s but no box_owner yet — ignoring", chat_id)
                return
            if not actor.get("user_id") or str(actor["user_id"]) != str(box_owner["user_id"]):
                LOG.info(
                    "my_chat_member chat_id=%s by user_id=%s (not box owner) — ignoring",
                    chat_id,
                    actor.get("user_id") or "?",
                )
                return
            new_status = ((update.get("new_chat_member") or {}).get("status")) or ""
            if new_status in ("left", "kicked"):
                LOG.info("my_chat_member chat_id=%s status=%s — not auto-allowing", chat_id, new_status)
                return
            LOG.info(
                "auto-allow chat_id=%s via my_chat_member (added by owner user_id=%s)",
                chat_id,
                actor["user_id"],
            )
            self._auto_allow_chat(chat_id, actor, announce=True)
        except Exception:
            LOG.exception("my_chat_member handling failed")

    # ----- Attachments -----

    def _download_telegram_file(self, file_id: str, suffix: str) -> str | None:
        """Pull a TG attachment to /home/bux/inbox and return the local path.

        TG's two-step model: getFile to resolve `file_id` → server-side
        `file_path`, then GET https://api.telegram.org/file/bot<token>/<path>
        for the bytes. Files >20MB aren't downloadable via this API.
        """
        try:
            info = self.call("getFile", file_id=file_id)
            if not info.get("ok"):
                return None
            file_path = info.get("result", {}).get("file_path", "")
            file_size = info.get("result", {}).get("file_size", 0) or 0
            if not file_path:
                return None
            if file_size and file_size > TG_MAX_DOWNLOAD_BYTES:
                LOG.info("skipping download: %d bytes > cap", file_size)
                return None
            url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
            r = self.client.get(url, timeout=60)
            r.raise_for_status()
            data = r.content
        except Exception:
            LOG.exception("telegram file download failed")
            return None

        inbox = "/home/bux/inbox"
        try:
            os.makedirs(inbox, exist_ok=True)
            uid, gid = _bux_uid_gid()
            os.chown(inbox, uid, gid)
        except Exception:
            LOG.exception("inbox setup failed")
        fname = f"{int(time.time())}-{file_id[:12]}{suffix}"
        path = f"{inbox}/{fname}"
        try:
            with open(path, "wb") as f:
                f.write(data)
            uid, gid = _bux_uid_gid()
            os.chown(path, uid, gid)
            os.chmod(path, 0o644)
        except Exception:
            LOG.exception("writing %s failed", path)
            return None
        return path

    def _transcribe_media(self, file_id: str, filename: str) -> tuple[str | None, str | None]:
        """Resolve a TG file_id, download the bytes, send to Whisper.

        Returns (transcript, error). Exactly one is non-None.
        """
        api_key = _load_openai_key()
        if not api_key:
            return (
                None,
                "❌ voice transcription unavailable — drop `OPENAI_API_KEY=...` into "
                "`/home/bux/.secrets/openai.env`.",
            )

        # 1) getFile → file_path on TG's CDN.
        gf = self.call("getFile", file_id=file_id)
        if not gf.get("ok"):
            return None, "❌ Telegram getFile failed; try resending the audio."
        file_path = (gf.get("result") or {}).get("file_path")
        if not file_path:
            return None, "❌ Telegram returned no file_path; try resending."

        # 2) Download bytes from TG's file CDN.
        dl_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        try:
            r = self.client.get(dl_url, timeout=60)
            r.raise_for_status()
            audio_bytes = r.content
        except Exception as e:
            LOG.warning("TG file download failed: %s", e)
            return None, f"❌ couldn't download the audio from Telegram: {e}"

        # 3) POST to Whisper. multipart/form-data with `file` and `model`.
        try:
            resp = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (filename, audio_bytes)},
                data={"model": "whisper-1"},
                timeout=60,
            )
            resp.raise_for_status()
            text = (resp.json() or {}).get("text") or ""
            text = text.strip()
            if not text:
                return None, "❌ Whisper returned empty text — couldn't make out anything."
            return text, None
        except httpx.HTTPStatusError as e:
            body = ""
            try:
                body = e.response.text[:300]
            except Exception:
                pass
            LOG.warning("whisper failed: %s body=%s", e, body)
            return None, f"❌ Whisper transcription failed (HTTP {e.response.status_code})."
        except Exception as e:
            LOG.warning("whisper failed: %s", e)
            return None, f"❌ Whisper transcription failed: {e}"

    def _extract_attachment(self, msg: dict) -> tuple[str | None, str]:
        """Return (path-on-disk, prompt-prefix) for any image/doc attachment."""
        photos = msg.get("photo") or []
        if photos:
            file_id = photos[-1].get("file_id")
            if file_id:
                path = self._download_telegram_file(file_id, ".jpg")
                if path:
                    return path, f"User sent an image at {path}. "
        doc = msg.get("document") or {}
        if doc.get("file_id"):
            fname = doc.get("file_name") or ""
            suffix = ""
            if "." in fname:
                suffix = "." + fname.rsplit(".", 1)[1]
            path = self._download_telegram_file(doc["file_id"], suffix or ".bin")
            if path:
                return path, f"User sent a file at {path}. "
        return None, ""

    # ----- Inbound handling -----

    def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        # Skip chat-lifecycle service messages (topic created, member joined,
        # title changed, …). They have no user intent and shouldn't bounce a
        # reply or auto-bind an unbound chat.
        if any(k in msg for k in _SERVICE_MESSAGE_FIELDS):
            return
        thread_id_raw = msg.get("message_thread_id")
        thread_id = thread_id_raw if isinstance(thread_id_raw, int) else 0

        # TG service messages (topic created/edited/closed, members joined,
        # pinned, video chat lifecycle, etc.) carry no user content — drop
        # silently before any binding or response logic. Without this we
        # reply "I don't know how to handle that" every time the user spins
        # up a new forum topic.
        if any(k in msg for k in _SERVICE_MSG_KEYS):
            LOG.debug(
                "dropping service message chat=%s keys=%s",
                chat_id,
                [k for k in _SERVICE_MSG_KEYS if k in msg],
            )
            return

        text = (msg.get("text") or "").strip()
        caption = (msg.get("caption") or "").strip()
        if caption and not text:
            text = caption
        mid = msg.get("message_id")
        sender = _extract_sender(msg)
        allow = load_allow()

        # Binding: first message wins. Topic-id is irrelevant — once the
        # parent chat is bound, every topic in it is automatically allowed.
        if chat_id not in allow:
            if self.setup_token:
                LOG.info("binding chat_id=%s (first-message wins)", chat_id)
                self._bind_chat(chat_id, sender=sender)
                return
            # Setup-token already burned. The ONLY path to allow-list a
            # new chat is `my_chat_member` firing with `from.id` matching
            # the box owner — i.e., the owner explicitly added the bot
            # via Telegram's add-to-group UI. Owner-sent messages in an
            # unallowed chat are NOT a fallback: that path used to admit
            # chats where someone else added the bot (with the owner as
            # a member) the moment the owner spoke, which leaks the bot
            # into chats the owner never opted into. To recover from a
            # missed my_chat_member update, owner must remove + re-add
            # the bot (or hand-edit /etc/bux/tg-allowed.txt).
            LOG.info("dropping msg from chat_id=%s (not allow-listed)", chat_id)
            return

        # Backfill the owner for chats bound before owner-tracking existed:
        # first sender we see in an allowed-but-unowned chat becomes the
        # owner. _set_owner_for no-ops if a record already exists.
        if sender.get("user_id") and not _owner_for(chat_id, self.state):
            _set_owner_for(chat_id, sender, self.state)

        # Voice / audio / video_note: download from TG, transcribe with Whisper,
        # then fall through to the normal text-message pipeline as if the user
        # had typed the transcript. Caption already became `text` above; in
        # that case we skip transcription.
        file_id, filename, file_size = _extract_media(msg)
        if file_id and not text:
            if file_size and file_size > TG_MAX_DOWNLOAD_BYTES:
                self.send(
                    chat_id,
                    f"❌ file is {file_size // (1024 * 1024)} MB — Telegram caps bot "
                    "downloads at 20 MB. Send a shorter clip.",
                    reply_to=mid,
                    thread_id=thread_id,
                )
                return
            self.typing(chat_id, thread_id=thread_id)
            self.send(chat_id, "🎤 transcribing…", reply_to=mid, thread_id=thread_id)
            transcript, err = self._transcribe_media(file_id, filename)
            if err is not None or not transcript:
                self.send(
                    chat_id,
                    err or "❌ transcription failed.",
                    reply_to=mid,
                    thread_id=thread_id,
                )
                return
            self.send(chat_id, f"📝 {transcript}", reply_to=mid, thread_id=thread_id)
            text = transcript

        key = _lane_key(chat_id, thread_id)
        slug = _lane_slug(key)

        owner = _owner_for(chat_id, self.state)
        cmd, arg = _parse_command(text)

        # `/terminal` — owner-only mode switch. Spawns a persistent bash
        # in this lane; from this point on plain-text messages route to
        # its stdin. `/terminal <initial cmd>` seeds the first command,
        # so `/terminal gh auth login` starts the shell + runs gh auth login
        # in one shot. `/terminal` while a session is already alive is a
        # no-op (the user said so) — protects an active session from a
        # fat-fingered re-trigger.
        if cmd == "/terminal":
            if not _is_owner(sender, owner):
                self.send(
                    chat_id,
                    "❌ `/terminal` is owner-only.",
                    reply_to=mid,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            existing = _get_shell_session(slug)
            if existing is not None:
                self.send(
                    chat_id,
                    "💻 terminal session already running here — replies go to its "
                    "stdin. Use /interrupt for Ctrl-C, /enter for a blank Enter, "
                    "/exit to ask bash to close, or /cancel to hard-kill.",
                    reply_to=mid,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            try:
                sess = ShellSession(
                    self,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    slug=slug,
                    initial_cmd=arg.strip() or None,
                    reply_to=mid,
                )
                sess.start()
            except Exception as e:
                LOG.exception("shell start failed for %s", slug)
                self.send(
                    chat_id,
                    f"❌ failed to start terminal: {e}",
                    reply_to=mid,
                    thread_id=thread_id,
                )
            return

        # `/exit` — graceful close. Writes `exit\n` to the bash so any
        # nested process gets a chance to clean up; bash exits, the
        # reader sees EOF, the teardown footer fires. /cancel remains
        # the hard-kill path. Outside a session, /exit is a no-op with
        # a hint.
        if cmd == "/exit":
            sess = _get_shell_session(slug)
            if sess is None:
                self.send(
                    chat_id,
                    "No terminal session here. `/terminal` to start one.",
                    reply_to=mid,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            if not _is_owner(sender, owner):
                self.send(
                    chat_id,
                    "❌ `/exit` is owner-only.",
                    reply_to=mid,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            sess.send_input("exit")
            return

        # `/interrupt` — send Ctrl-C to the foreground process in the active
        # PTY. This is the right escape hatch for auth flows or interactive
        # commands that swallowed `/exit` as ordinary input; it keeps the
        # terminal session alive so the user can continue at the shell prompt.
        if cmd in ("/interrupt", "/ctrlc", "/c"):
            sess = _get_shell_session(slug)
            if sess is None:
                self.send(
                    chat_id,
                    "No terminal session here. `/terminal` to start one.",
                    reply_to=mid,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            if not _is_owner(sender, owner):
                self.send(
                    chat_id,
                    "❌ `/interrupt` is owner-only.",
                    reply_to=mid,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            if sess.send_bytes(b"\x03"):
                self.react(chat_id, mid, "✍")
            else:
                self.send(
                    chat_id,
                    "❌ terminal session is gone — Ctrl-C not delivered.",
                    reply_to=mid,
                    thread_id=thread_id,
                )
            return

        # `/enter` — send a bare newline to the PTY. Telegram cannot send an
        # empty text message, but some auth flows ask the user to press Enter
        # after a browser step.
        if cmd in ("/enter", "/return"):
            sess = _get_shell_session(slug)
            if sess is None:
                self.send(
                    chat_id,
                    "No terminal session here. `/terminal` to start one.",
                    reply_to=mid,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            if not _is_owner(sender, owner):
                self.send(
                    chat_id,
                    "❌ `/enter` is owner-only.",
                    reply_to=mid,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            if sess.send_bytes(b"\n"):
                self.react(chat_id, mid, "✍")
            else:
                self.send(
                    chat_id,
                    "❌ terminal session is gone — Enter not delivered.",
                    reply_to=mid,
                    thread_id=thread_id,
                )
            return

        # `/eof` — send Ctrl-D to the PTY. Useful for programs waiting on
        # stdin EOF; if bash is at an empty prompt it exits the session.
        if cmd in ("/eof", "/ctrld", "/d"):
            sess = _get_shell_session(slug)
            if sess is None:
                self.send(
                    chat_id,
                    "No terminal session here. `/terminal` to start one.",
                    reply_to=mid,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            if not _is_owner(sender, owner):
                self.send(
                    chat_id,
                    "❌ `/eof` is owner-only.",
                    reply_to=mid,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            if sess.send_bytes(b"\x04"):
                self.react(chat_id, mid, "✍")
            else:
                self.send(
                    chat_id,
                    "❌ terminal session is gone — Ctrl-D not delivered.",
                    reply_to=mid,
                    thread_id=thread_id,
                )
            return

        # If a shell session is active in this lane, plain-text messages
        # are stdin for it (codex login codes, gh auth login codes, `read`
        # answers, `y/n` prompts, …). Slash commands fall through below
        # so the user can still `/cancel`, `/queue`, etc.
        if not text.startswith("/"):
            sess = _get_shell_session(slug)
            if sess is not None and _is_owner(sender, owner):
                if sess.send_input(text):
                    self.react(chat_id, mid, "✍")
                else:
                    self.send(
                        chat_id,
                        "❌ terminal session is gone — message not delivered.",
                        reply_to=mid,
                        thread_id=thread_id,
                    )
                return
        if cmd in ("/start", "/help"):
            self.send(
                chat_id,
                "Text me anything — I'll run it on your bux.\n\n"
                "Forum topics each get their own agent session and run in "
                "parallel — no concurrency cap, only the box's RAM gates it.\n\n"
                "Commands\n"
                "/terminal — open an interactive shell here (owner-only); "
                "replies route to stdin until you `exit` or send /exit. "
                "/terminal <cmd> seeds the first command, e.g. `/terminal gh auth login`\n"
                "/interrupt — send Ctrl-C to the active terminal session\n"
                "/enter — send Enter to the active terminal session\n"
                "/eof — send Ctrl-D to the active terminal session\n"
                "/exit — ask bash to close the active terminal session\n"
                "/codex — switch this topic to Codex\n"
                "/codex login — sign in Codex with device auth\n"
                "/codex logout — sign out Codex\n"
                "/claude — switch this topic to Claude\n"
                "/claude login — sign in Claude through a terminal flow\n"
                "/claude logout — sign out Claude\n"
                "/agent claude|codex — switch this topic to a different agent\n"
                "/live — live-view URL of the active browser\n"
                "/queue — pending tasks in this topic\n"
                "/cancel — kill the running task / terminal + drop "
                "everything pending in this topic\n"
                "/cancel <id> — cancel one task (running or queued)\n"
                "/compact — summarize this topic's agent session to free up context\n"
                "/schedules — list reminders / cron jobs\n"
                "/login — auth status / connect a service (e.g. /login github, /login claude, /login codex)\n"
                "/logout — disconnect a service (e.g. /logout github, /logout claude, /logout codex)\n"
                "/swipe — open the agency swipe deck (Tinder-style mini app)\n"
                "/version — show the bux agent version\n"
                "/update — pull latest code + restart (or /update <branch>)",
                reply_to=mid,
                thread_id=thread_id,
            )
            return
        if cmd == "/whoami":
            who_label = _sender_label(sender) or "(unknown)"
            who_id = sender.get("user_id") or "?"
            owner = _owner_for(chat_id, self.state)
            if owner:
                role = "owner" if _is_owner(sender, owner) else "guest"
                owner_line = (
                    f"role: {role}\n"
                    f"owner: {_sender_label(owner) or '(unknown)'} "
                    f"(id `{owner.get('user_id', '?')}`)"
                )
            else:
                owner_line = "role: (no owner recorded for this chat)"
            self.send(
                chat_id,
                f"chat_id: {chat_id}\nthread_id: {thread_id}\nlane: `{slug}`\n"
                f"agent: `{_agent_for(key, self.state)}`\n"
                f"you: {who_label} (id `{who_id}`)\n"
                f"{owner_line}",
                reply_to=mid,
                thread_id=thread_id,
                markdown=True,
            )
            return
        if cmd == "/live":
            self.send(chat_id, self._live_url(), reply_to=mid, thread_id=thread_id)
            return
        if cmd == "/queue":
            self._cmd_queue(slug, chat_id, mid, thread_id)
            return
        if cmd == "/cancel":
            self._cmd_cancel(slug, chat_id, mid, thread_id, arg)
            return
        if cmd in ("/schedules", "/schedule"):
            self._cmd_schedules(chat_id, mid, thread_id)
            return
        if cmd == "/agent":
            self._cmd_agent(key, chat_id, mid, thread_id, arg)
            return
        if cmd == "/codex":
            action = arg.strip().lower()
            if action in ("login", "auth"):
                self._start_login_provider(
                    "codex", CODEX_AUTH_PROVIDER, chat_id, mid, thread_id
                )
                return
            if action in ("logout", "disconnect"):
                self._cmd_provider_logout(
                    "codex", CODEX_AUTH_PROVIDER, chat_id, mid, thread_id, sender, owner
                )
                return
            self._cmd_agent(key, chat_id, mid, thread_id, AGENT_CODEX)
            return
        if cmd == "/claude":
            action = arg.strip().lower()
            if action in ("login", "auth"):
                self._cmd_claude_login(chat_id, mid, thread_id, slug, sender, owner)
                return
            if action in ("logout", "disconnect"):
                self._cmd_claude_logout(chat_id, mid, thread_id, sender, owner)
                return
            self._cmd_agent(key, chat_id, mid, thread_id, AGENT_CLAUDE)
            return
        if cmd in ("/swipe", "/agency"):
            self._cmd_swipe(chat_id, mid, thread_id)
            return
        if cmd == "/version":
            self._cmd_version(chat_id, mid, thread_id)
            return
        if cmd == "/update":
            self._cmd_update(chat_id, mid, thread_id, arg)
            return
        if cmd == "/login":
            self._cmd_login(chat_id, mid, thread_id, arg, slug=slug, sender=sender, owner=owner)
            return
        if cmd == "/logout":
            self._cmd_logout(chat_id, mid, thread_id, arg, sender=sender, owner=owner)
            return
        if cmd == "/compact":
            # Forward to the lane's agent as a slash command. Both claude
            # and codex interpret `/compact` natively and write the compacted
            # state back to their session/thread storage, so later resume
            # turns start from the summary.
            #
            # `/compact <instructions>` is supported by the CLIs: trailing
            # text becomes guidance for what to focus on when summarizing.
            # We pass it through verbatim.
            # Rewrite the prompt and let the rest of handle() enqueue it
            # like a normal turn — the streaming bubble will surface
            # whatever summary the active agent emits. Preserve the trailing
            # arg so `/compact focus on the bug fixes` reaches the CLI with
            # its instructions intact.
            text = "/compact" + (" " + arg if arg else "")

        # Attachment + caption combo: download synchronously (small file) and
        # inject a path reference into the prompt so the agent can read it.
        attachment_path, attach_prefix = self._extract_attachment(msg)
        if attachment_path is None and not text:
            has_attachment = bool(msg.get("photo") or msg.get("document"))
            if has_attachment:
                self.send(
                    chat_id,
                    "Couldn't download that attachment — TG's max for bots is 20 MB. "
                    "Send a smaller copy or a link?",
                    reply_to=mid,
                    thread_id=thread_id,
                )
                return
            # Sticker / contact / location / poll / dice / etc. — none of which
            # we can usefully forward to claude. Acknowledge politely instead
            # of dispatching a "look at the attached file" prompt with no
            # actual file behind it.
            self.send(
                chat_id,
                "I don't know how to handle that — text, photos, documents, and voice notes only.",
                reply_to=mid,
                thread_id=thread_id,
            )
            return
        # When the user explicitly replied to a previous message, surface
        # that context to the agent so it knows what's being followed up on.
        # TG only sets `reply_to_message` on explicit replies — sending a
        # fresh message into a forum topic doesn't trip this. Service-event
        # quotes (topic creation, etc.) carry no text/caption, so the empty
        # snippet check below filters them out.
        reply_prefix = ""
        quoted = msg.get("reply_to_message") or {}
        quoted_text = (quoted.get("text") or quoted.get("caption") or "").strip()
        quoted_text = " ".join(quoted_text.split())
        if quoted_text:
            snippet = quoted_text[:20]
            if len(quoted_text) > 20:
                snippet += "…"
            reply_prefix = f'[Replying to: "{snippet}"] '
        final_prompt = (reply_prefix + attach_prefix + text).strip() or (
            "Look at the attached file and tell me what it is."
        )
        thinking_emoji = random_thinking_emoji()
        job = {
            "id": _new_job_id(),
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": mid,
            "prompt": final_prompt,
            "queued_at": time.time(),
            "status": "queued",
            "sender": sender,
            "thinking_emoji": thinking_emoji,
        }
        depth = _enqueue(slug, job, self._lane_drain)
        self.typing(chat_id, thread_id=thread_id)
        # Only ack when the user is actually queueing behind something —
        # the typing indicator + the placeholder bubble that StreamingMessage
        # sends when work actually starts are enough for "I see you".
        #
        # The ack carries an inline-keyboard "🚀 Steer" button (owner-only).
        # Tapping it kills the in-flight job and promotes this one to the
        # head of the queue — the new prompt then runs against the same
        # session UUID / codex thread so the agent keeps its prior tool
        # history and only the cancelled-turn's prompt is replaced.
        # Mirrors Claude Code's "Esc to cancel + new message" UX so a
        # typo / clarification doesn't have to wait for a long turn.
        if depth > 1:
            steer_kbd = {
                "inline_keyboard": [[{
                    "text": "🚀 Steer (kill running, run this next)",
                    "callback_data": f"steer:{slug}:{job['id']}",
                }]],
            }
            steer_mid = self.send_returning_id(
                chat_id,
                f"{thinking_emoji} queued (#{depth})",
                reply_to=mid,
                thread_id=thread_id,
                reply_markup=steer_kbd,
            )
            if isinstance(steer_mid, int):
                _lane_set_steer_msg_id(slug, job["id"], steer_mid)

    def _cmd_queue(self, slug: str, chat_id: int, reply_to: int | None, thread_id: int) -> None:
        jobs = _snapshot_lane(slug)
        if not jobs:
            self.send(chat_id, "Queue is empty.", reply_to=reply_to, thread_id=thread_id)
            return
        lines = ["Pending in this topic:"]
        for j in jobs:
            marker = "▶" if j.get("status") == "in_flight" else "·"
            preview = (j.get("prompt") or "").strip().splitlines()[0] if j.get("prompt") else ""
            if len(preview) > 60:
                preview = preview[:57] + "…"
            lines.append(f"{marker} `{j['id']}` — {preview}")
        self.send(
            chat_id,
            "\n".join(lines),
            reply_to=reply_to,
            thread_id=thread_id,
            markdown=True,
        )

    def _cmd_cancel(
        self,
        slug: str,
        chat_id: int,
        reply_to: int | None,
        thread_id: int,
        job_id: str,
    ) -> None:
        # Per-lane cancel. The original design preserved in-flight ("can't
        # safely kill mid-task") but in practice the only time users hit
        # /cancel is when the agent is wedged on an interactive child
        # (gh auth login, ssh, vim) — preserving the wedge made the bot
        # unusable until bux-tg was manually restarted. Now /cancel
        # SIGKILLs the in-flight proc *for this slug only* so other lanes
        # keep running.
        if not job_id:
            dropped = _drop_all_queued(slug)
            killed_in_flight = False
            with _inflight_lock:
                proc = _inflight_procs.get(slug)
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                        killed_in_flight = True
                    except Exception:
                        LOG.exception("failed to kill in-flight agent for lane %s", slug)
            killed_shell = False
            shell_sess = _get_shell_session(slug)
            if shell_sess is not None:
                shell_sess.kill("cancelled")
                killed_shell = True
            if dropped == 0 and not killed_in_flight and not killed_shell:
                self.send(
                    chat_id,
                    "Nothing to cancel.",
                    reply_to=reply_to,
                    thread_id=thread_id,
                )
                return
            parts: list[str] = []
            if killed_in_flight:
                parts.append("killed running task")
            if killed_shell:
                parts.append("killed shell session")
            if dropped > 0:
                parts.append(f"cancelled {dropped} pending task(s)")
            self.send(
                chat_id,
                "✓ " + " + ".join(parts) + ".",
                reply_to=reply_to,
                thread_id=thread_id,
            )
            return
        removed = _remove_queued(slug, job_id)
        if removed is None:
            self.send(
                chat_id,
                f"No pending task with id `{job_id}`.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
        elif removed.get("status") == "in_flight":
            # `/cancel <id>` against the running task — same SIGKILL path
            # as bare `/cancel`, just scoped to this single id. We already
            # know the id is the in-flight one for this slug because
            # `_remove_queued` returned its row.
            killed = False
            with _inflight_lock:
                proc = _inflight_procs.get(slug)
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                        killed = True
                    except Exception:
                        LOG.exception("failed to kill in-flight agent for lane %s", slug)
            if killed:
                self.send(
                    chat_id,
                    f"🛑 Cancelled `{job_id}`.",
                    reply_to=reply_to,
                    thread_id=thread_id,
                    markdown=True,
                )
            else:
                self.send(
                    chat_id,
                    f"Task `{job_id}` finished before we could kill it.",
                    reply_to=reply_to,
                    thread_id=thread_id,
                    markdown=True,
                )
        else:
            self.send(
                chat_id,
                f"Cancelled task `{job_id}`.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )

    def _cmd_schedules(self, chat_id: int, reply_to: int | None, thread_id: int) -> None:
        """List the bux user's pending `at` jobs and crontab. Read-only —
        users ask claude to cancel, since claude has the context to map
        \"the slack one\" to a job id."""
        lines: list[str] = []

        try:
            atq_out = subprocess.run(
                ["sudo", "-u", "bux", "atq"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except Exception:
            LOG.exception("atq failed")
            atq_out = ""

        at_rows: list[tuple[str, str, str]] = []
        for row in atq_out.splitlines():
            parts = row.split("\t") if "\t" in row else row.split()
            if not parts:
                continue
            job_id = parts[0]
            fire_time = " ".join(parts[1:-2]) if len(parts) >= 4 else " ".join(parts[1:])
            body = ""
            try:
                dump = subprocess.run(
                    ["sudo", "-u", "bux", "at", "-c", job_id],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout
                for ln in reversed([x for x in dump.splitlines() if x.strip()]):
                    if ln.strip().startswith("}"):
                        continue
                    body = ln.strip()
                    break
            except Exception:
                LOG.exception("at -c %s failed", job_id)
            at_rows.append((job_id, fire_time, body))

        if at_rows:
            lines.append("🕒 *Pending reminders*")
            for job_id, fire_time, body in at_rows:
                preview = body if len(body) <= 70 else body[:67] + "…"
                lines.append(f"· `{job_id}` — {fire_time}\n  {preview}")

        try:
            cron_out = subprocess.run(
                ["sudo", "-u", "bux", "crontab", "-l"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except Exception:
            LOG.exception("crontab -l failed")
            cron_out = ""

        cron_rows = [
            ln for ln in cron_out.splitlines() if ln.strip() and not ln.strip().startswith("#")
        ]
        if cron_rows:
            if lines:
                lines.append("")
            lines.append("🔁 *Recurring*")
            for ln in cron_rows:
                preview = ln.strip()
                if len(preview) > 100:
                    preview = preview[:97] + "…"
                lines.append(f"· {preview}")

        if not lines:
            self.send(chat_id, "Nothing scheduled.", reply_to=reply_to, thread_id=thread_id)
            return
        lines.append("")
        lines.append('_To cancel: ask claude ("cancel the 9am reminder")._')
        self.send(
            chat_id,
            "\n".join(lines),
            reply_to=reply_to,
            thread_id=thread_id,
            markdown=True,
        )

    def _cmd_agent(
        self,
        key: LaneKey,
        chat_id: int,
        reply_to: int | None,
        thread_id: int,
        arg: str,
    ) -> None:
        arg = arg.strip().lower()
        current = _agent_for(key, self.state)
        if not arg:
            self.send(
                chat_id,
                f"This topic is using `{current}`.\n\nUse `/claude` or `/codex` "
                "to switch.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        if arg not in AGENTS:
            self.send(
                chat_id,
                f"Unknown agent `{arg}`. Pick: " + " / ".join(f"`{a}`" for a in AGENTS),
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        if arg == current:
            self.send(
                chat_id,
                f"Already on `{arg}`.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        _set_agent_for(key, arg, self.state)
        self.send(
            chat_id,
            f"Switched to `{arg}` for this topic. Each agent has its own "
            "session UUID and workspace.",
            reply_to=reply_to,
            thread_id=thread_id,
            markdown=True,
        )

    # ----- Lane worker -----

    def _lane_drain(self, slug: str) -> None:
        """Single-lane FIFO drainer. Holds the cross-lane semaphore only while
        actually running a task; releases it between jobs so other lanes can
        grab a slot. The outer try/finally guarantees this lane's worker
        entry is removed from `_lane_workers` even if an unhandled exception
        kills the loop — otherwise a future enqueue would see the dead
        thread reference and never spawn a replacement, starving the lane."""
        LOG.info("lane %s drain start", slug)
        try:
            while True:
                with _lanes_lock:
                    job = _pop_next_locked(slug)
                    if job is None:
                        # Pop the worker entry while still holding the lock so
                        # a concurrent _enqueue can't see this dying thread
                        # and skip spawning a replacement.
                        _lane_workers.pop(slug, None)
                        LOG.info("lane %s drain exit (empty)", slug)
                        return
                job_id = str(job.get("id") or "?")
                chat_id = job.get("chat_id")
                thread_id = job.get("thread_id") or 0
                mid = job.get("message_id")
                prompt = str(job.get("prompt") or "")
                thinking_emoji = job.get("thinking_emoji")
                if not isinstance(thinking_emoji, str) or not thinking_emoji:
                    thinking_emoji = None
                if not isinstance(chat_id, int):
                    LOG.warning("lane %s job %s missing chat_id; skipping", slug, job_id)
                    with _lanes_lock:
                        _finish_locked(slug, job_id)
                    continue
                key: LaneKey = (chat_id, thread_id if isinstance(thread_id, int) else 0)
                sender = job.get("sender") if isinstance(job.get("sender"), dict) else None
                # If this job was queued behind something and we exposed
                # a Steer button on its ack bubble, strip the button now
                # that it's running — a stale tap can't usefully kill
                # the very job it would have promoted.
                steer_mid = job.get("steer_msg_id")
                if isinstance(steer_mid, int):
                    try:
                        self.call("editMessageReplyMarkup", chat_id=chat_id,
                                  message_id=steer_mid,
                                  reply_markup={"inline_keyboard": []})
                    except Exception:
                        LOG.exception("strip steer keyboard for %s failed", job_id)
                try:
                    self.typing(chat_id, thread_id=key[1])
                    self.run_task(
                        key,
                        prompt,
                        reply_to=mid if isinstance(mid, int) else None,
                        sender=sender,
                        thinking_emoji=thinking_emoji,
                    )
                except Exception:
                    LOG.exception("lane %s job %s failed", slug, job_id)
                    try:
                        self.react(chat_id, mid if isinstance(mid, int) else None, EMOJI_ERROR)
                    except Exception:
                        pass
                finally:
                    with _lanes_lock:
                        _finish_locked(slug, job_id)
        finally:
            # Always release the worker slot so the next enqueue spawns a
            # replacement — even if `_pop_next_locked` raised mid-loop.
            with _lanes_lock:
                _lane_workers.pop(slug, None)

    # ----- Agency mini app -----

    def _cmd_swipe(self, chat_id: int, reply_to: int | None, thread_id: int) -> None:
        """Send the agency-mini-app launcher button into this topic.

        The URL is set by bootstrap once the cloudflared tunnel comes up; if
        it isn't there yet, surface a hint instead of a dead button.
        """
        url = (_read_kv(BOX_ENV).get("BUX_AGENCY_APP_URL") or "").strip()
        if not url:
            self.send(
                chat_id,
                "Agency mini app isn't reachable yet — `BUX_AGENCY_APP_URL` "
                "is unset. Run /update once the tunnel is provisioned.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        markup = {
            "inline_keyboard": [[
                {"text": "🃏 Open agency", "web_app": {"url": url}},
            ]]
        }
        self.send(
            chat_id,
            "Swipe right to dispatch, left to dismiss, up for feedback.",
            reply_to=reply_to,
            thread_id=thread_id,
            reply_markup=markup,
        )

    # ----- Self-update -----

    def _cmd_version(self, chat_id: int, reply_to: int | None, thread_id: int) -> None:
        """Report the agent's git SHA + branch + last-commit-line.

        Lets the user check "what version is my box on" without having to ssh
        in or open the cloud admin UI. Reads straight from the cloned OSS
        repo at /opt/bux/repo.
        """
        repo = "/opt/bux/repo"
        try:
            sha = (
                subprocess.run(
                    ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                ).stdout.strip()
                or "unknown"
            )
            branch = (
                subprocess.run(
                    ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                ).stdout.strip()
                or "unknown"
            )
            last = (
                subprocess.run(
                    ["git", "-C", repo, "log", "-1", "--pretty=%h %s"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                ).stdout.strip()
                or "(no log)"
            )
            ahead_behind = ""
            rc = subprocess.run(
                ["git", "-C", repo, "fetch", "--quiet", "origin", branch],
                capture_output=True,
                timeout=10,
            ).returncode
            if rc == 0:
                ab = (
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            repo,
                            "rev-list",
                            "--left-right",
                            "--count",
                            f"HEAD...origin/{branch}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    .stdout.strip()
                    .split()
                )
                if len(ab) == 2:
                    ahead, behind = ab
                    if behind != "0":
                        ahead_behind = f" · *{behind} commits behind* (run /update to catch up)"
                    elif ahead != "0":
                        ahead_behind = f" · {ahead} commits ahead of origin"
        except Exception:
            LOG.exception("/version failed")
            self.send(chat_id, "Could not read version.", reply_to=reply_to, thread_id=thread_id)
            return
        body = (
            f"*bux* on `{branch}`\n"
            f"`{sha}` — {last}{ahead_behind}\n\n"
            "_Source: github.com/browser-use/bux_"
        )
        self.send(chat_id, body, reply_to=reply_to, thread_id=thread_id, markdown=True)

    def _cmd_update(self, chat_id: int, reply_to: int | None, thread_id: int, branch: str) -> None:
        """Pull latest agent code from OSS and restart services.

        Branch defaults to whatever the box is tracking (`main` for now). Pass
        `/update <branch>` to switch tracks (e.g. /update stable).

        The restart kills this very process, so we send the ack BEFORE invoking
        bootstrap.sh. The new agent comes up within ~10s and the user's next
        message lands fine.
        """
        repo = "/opt/bux/repo"
        target = (
            (branch or "").strip()
            or subprocess.run(
                ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            or "main"
        )

        self.send(
            chat_id,
            f"⏳ Updating to latest `{target}`…",
            reply_to=reply_to,
            thread_id=thread_id,
            markdown=True,
        )

        try:
            # Widen the fetch refspec to all branches if it isn't already.
            # install.sh clones with --branch main, leaving a single-branch
            # remote that can't reach feature branches by name. Idempotent.
            subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "config",
                    "--replace-all",
                    "remote.origin.fetch",
                    "+refs/heads/*:refs/remotes/origin/*",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            r = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "fetch",
                    "--prune",
                    "origin",
                    f"+refs/heads/{target}:refs/remotes/origin/{target}",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if r.returncode != 0:
                self.send(
                    chat_id,
                    f"❌ git fetch failed: {r.stderr[:300]}",
                    reply_to=reply_to,
                    thread_id=thread_id,
                )
                return
            # checkout -B (not reset --hard) so HEAD's symbolic-ref points at
            # the requested branch. reset --hard moves whatever-branch-we're-
            # on to the target commit without switching branches — so /version
            # still reports the old branch name after update.
            r = subprocess.run(
                ["git", "-C", repo, "checkout", "-B", target, "--track", f"origin/{target}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if r.returncode != 0:
                self.send(
                    chat_id,
                    f"❌ git checkout failed: {r.stderr[:300]}",
                    reply_to=reply_to,
                    thread_id=thread_id,
                )
                return
            new_sha = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            self.send(
                chat_id,
                f"✓ Pulled `{new_sha}`. Restarting bux…",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            # Mark this lane as having requested the restart, so the post-boot
            # announce sends it a "back online" confirmation even if the lane
            # is idle (the default announce only pings lanes with pending work).
            try:
                UPDATE_REQUEST_LANES.parent.mkdir(parents=True, exist_ok=True)
                with UPDATE_REQUEST_LANES.open("a") as f:
                    f.write(f"{chat_id}\t{thread_id or ''}\n")
            except Exception:
                LOG.exception("failed to record update-request lane")
            # bux-tg.service runs as root so this is direct — no sudo needed.
            # Fire-and-forget; the restart kills us before we'd wait.
            subprocess.Popen(
                ["/bin/bash", f"{repo}/agent/bootstrap.sh"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            LOG.exception("/update failed")
            self.send(
                chat_id,
                f"❌ update failed: {e}",
                reply_to=reply_to,
                thread_id=thread_id,
            )

    def _cmd_login(
        self,
        chat_id: int,
        reply_to: int | None,
        thread_id: int,
        arg: str,
        slug: str | None = None,
        sender: dict | None = None,
        owner: dict | None = None,
    ) -> None:
        """`/login` — list providers + status. `/login <name>` — start flow.

        The actual login runs in a background thread because device-code
        flows block until the user authorizes (gh polls for ~15min). We
        don't want the bot's main poll loop stuck waiting; instead we
        send progress messages from the worker thread, all routed back
        into the same forum topic via thread_id.
        """
        name = _normalize_login_provider_name(arg)
        if not name:
            # List status of every registered provider.
            lines = ["*Auth status:*"]
            for pname, prov in AUTH_PROVIDERS.items():
                connected, status = prov.check()
                icon = "✓" if connected else "·"
                lines.append(f"{icon} `{pname}` — {status}")
            connected, status = _claude_login_status()
            icon = "✓" if connected else "·"
            lines.append(f"{icon} `claude` — {status}")
            lines.append("")
            lines.append("Use `/login <service>` to connect, e.g. `/login github`, `/login claude`, or `/login codex`.")
            self.send(
                chat_id,
                "\n".join(lines),
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        if name == "claude":
            if slug is None or sender is None:
                self.send(
                    chat_id,
                    "❌ `/login claude` is only available from Telegram.",
                    reply_to=reply_to,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            self._cmd_claude_login(chat_id, reply_to, thread_id, slug, sender, owner)
            return
        prov = AUTH_PROVIDERS.get(name)
        if prov is None:
            known = ", ".join([*AUTH_PROVIDERS.keys(), "claude"]) or "(none)"
            self.send(
                chat_id,
                f"Unknown provider `{name}`. Known: {known}.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        self._start_login_provider(name, prov, chat_id, reply_to, thread_id)

    def _start_login_provider(
        self,
        name: str,
        prov,
        chat_id: int,
        reply_to: int | None,
        thread_id: int,
        force: bool = False,
    ) -> None:
        # If already connected, short-circuit. Saves the user a redundant
        # device-code dance and prevents accidentally rotating their token.
        connected, status = prov.check()
        if connected and not force:
            self.send(
                chat_id,
                f"✓ `{name}` already connected ({status}). Use `/logout {name}` to disconnect first.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return

        def _on_progress(text: str, url: str | None = None, code: str | None = None) -> None:
            reply_markup = None
            if name == "codex" and url and code:
                reply_markup = _codex_login_reply_markup(url, code)
            self.send(
                chat_id,
                text,
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
                reply_markup=reply_markup,
            )

        def _runner() -> None:
            self.send(
                chat_id,
                f"Connecting to {prov.label}…",
                reply_to=reply_to,
                thread_id=thread_id,
            )
            try:
                ok, msg = prov.login(_on_progress)
            except Exception as e:
                LOG.exception("login %s failed", name)
                self.send(
                    chat_id,
                    f"❌ `{name}` login failed: {e}",
                    reply_to=reply_to,
                    thread_id=thread_id,
                    markdown=True,
                )
                return
            icon = "✓" if ok else "❌"
            self.send(
                chat_id,
                f"{icon} `{name}` {msg}",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )

        threading.Thread(target=_runner, name=f"bux-login-{name}", daemon=True).start()

    def _cmd_claude_login(
        self,
        chat_id: int,
        reply_to: int | None,
        thread_id: int,
        slug: str,
        sender: dict,
        owner: dict | None,
    ) -> None:
        if not _is_owner(sender, owner):
            self.send(
                chat_id,
                "❌ `/claude login` is owner-only.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        existing = _get_shell_session(slug)
        if existing is not None:
            self.send(
                chat_id,
                "💻 terminal session already running here. Paste into it, or use "
                "/interrupt, /exit, or /cancel first.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        try:
            sess = ShellSession(
                self,
                chat_id=chat_id,
                thread_id=thread_id,
                slug=slug,
                initial_cmd="claude auth login",
                reply_to=reply_to,
                auto_enter_after_input_sec=2.0,
                close_on_success_patterns=(
                    "login successful",
                    "successfully logged in",
                    "authenticated",
                ),
                success_message=(
                    "✓ Claude login successful. Terminal closed; agent is ready. "
                    "Send `hi` to start."
                ),
            )
            sess.start()
        except Exception as e:
            LOG.exception("claude login shell start failed for %s", slug)
            self.send(
                chat_id,
                f"❌ failed to start Claude login terminal: {e}",
                reply_to=reply_to,
                thread_id=thread_id,
            )

    def _cmd_provider_logout(
        self,
        name: str,
        prov,
        chat_id: int,
        reply_to: int | None,
        thread_id: int,
        sender: dict,
        owner: dict | None,
    ) -> None:
        if not _is_owner(sender, owner):
            self.send(
                chat_id,
                f"❌ `/{name} logout` is owner-only.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        try:
            ok, msg = prov.logout()
        except Exception as e:
            LOG.exception("%s logout failed", name)
            self.send(
                chat_id,
                f"❌ `{name}` logout failed: {e}",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        icon = "✓" if ok else "❌"
        self.send(
            chat_id,
            f"{icon} `{name}` {msg}",
            reply_to=reply_to,
            thread_id=thread_id,
            markdown=True,
        )

    def _cmd_claude_logout(
        self,
        chat_id: int,
        reply_to: int | None,
        thread_id: int,
        sender: dict,
        owner: dict | None,
    ) -> None:
        if not _is_owner(sender, owner):
            self.send(
                chat_id,
                "❌ `/claude logout` is owner-only.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        try:
            r = subprocess.run(
                ["sudo", "-iu", "bux", "claude", "auth", "logout"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as e:
            LOG.exception("claude logout failed")
            self.send(
                chat_id,
                f"❌ `claude` logout failed: {e}",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        out = _ANSI_RE.sub("", (r.stdout + r.stderr)).strip()
        if r.returncode == 0:
            self.send(
                chat_id,
                "✓ `claude` logged out" + (f" ({out})" if out else ""),
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
        else:
            self.send(
                chat_id,
                f"❌ `claude` logout failed: {out or f'rc={r.returncode}'}",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )

    def _cmd_logout(
        self,
        chat_id: int,
        reply_to: int | None,
        thread_id: int,
        arg: str,
        sender: dict | None = None,
        owner: dict | None = None,
    ) -> None:
        """`/logout` — list providers + status. `/logout <name>` — disconnect."""
        name = _normalize_login_provider_name(arg)
        if not name:
            # Same listing as /login (helps the user see what's currently
            # logged in without remembering which command they want).
            self._cmd_login(chat_id, reply_to, thread_id, "")
            return
        if name == "claude":
            self._cmd_claude_logout(chat_id, reply_to, thread_id, sender or {}, owner)
            return
        prov = AUTH_PROVIDERS.get(name)
        if prov is None:
            known = ", ".join([*AUTH_PROVIDERS.keys(), "claude"]) or "(none)"
            self.send(
                chat_id,
                f"Unknown provider `{name}`. Known: {known}.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        try:
            ok, msg = prov.logout()
        except Exception as e:
            LOG.exception("logout %s failed", name)
            self.send(
                chat_id,
                f"❌ `{name}` logout failed: {e}",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        icon = "✓" if ok else "❌"
        self.send(
            chat_id,
            f"{icon} `{name}` {msg}",
            reply_to=reply_to,
            thread_id=thread_id,
            markdown=True,
        )

    # ----- Live URL -----

    def _live_url(self) -> str:
        box_env = _read_kv(BOX_ENV)
        browser_env = _read_kv(BROWSER_ENV)
        api_key = box_env.get("BROWSER_USE_API_KEY")
        browser_id = browser_env.get("BU_BROWSER_ID")
        if not api_key:
            return "❌ no BROWSER_USE_API_KEY on this box"
        if not browser_id:
            return "❌ no active browser yet — keeper may still be starting"
        try:
            r = httpx.get(
                f"https://api.browser-use.com/api/v3/browsers/{browser_id}",
                headers={"X-Browser-Use-API-Key": api_key},
                timeout=10,
            )
            r.raise_for_status()
            live = r.json().get("liveUrl")
            if not live:
                return "❌ browser has no liveUrl (session may be stale)"
            return f"🖥 {live}"
        except Exception as e:
            return f"❌ live-url lookup failed: {e}"

    # ----- Poll loop -----

    def _handle_in_thread(self, msg: dict) -> None:
        try:
            self.handle(msg)
        except Exception:
            LOG.exception("handle failed")

    def poll_loop(self) -> None:
        LOG.info("bux-tg starting poll loop")
        while True:
            try:
                # allowed_updates filters server-side so we don't burn
                # update_ids on channel_post / inline_query / poll / etc —
                # we only consume message + edited_message + callback_query
                # + my_chat_member. callback_query is for the tg-approve
                # permission bridge (inline-keyboard taps on Allow/Deny
                # prompts). my_chat_member fires when the bot is
                # added/removed/promoted in a chat — used to auto-allow
                # new chats that the box owner adds the bot to.
                params: dict = {
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": [
                        "message", "edited_message", "callback_query", "my_chat_member",
                    ],
                }
                if self.state.get("offset"):
                    params["offset"] = self.state["offset"] + 1
                data = self.call("getUpdates", **params)
                updates = data.get("result", [])
                if updates:
                    self.state["offset"] = max(u["update_id"] for u in updates)
                    save_state(self.state)
                for u in updates:
                    if cb := u.get("callback_query"):
                        threading.Thread(
                            target=self._handle_callback_query,
                            args=(cb,),
                            daemon=True,
                        ).start()
                        continue
                    if mcm := u.get("my_chat_member"):
                        threading.Thread(
                            target=self._handle_my_chat_member,
                            args=(mcm,),
                            daemon=True,
                        ).start()
                        continue
                    msg = u.get("message") or u.get("edited_message")
                    if not msg:
                        continue
                    threading.Thread(
                        target=self._handle_in_thread,
                        args=(msg,),
                        daemon=True,
                    ).start()
            except httpx.HTTPError:
                LOG.exception("poll failed; sleep 5s")
                time.sleep(5)
            except Exception:
                LOG.exception("unexpected; sleep 5s")
                time.sleep(5)

    def _handle_callback_query(self, cb: dict) -> None:
        """Process an inline-keyboard tap from tg-approve's question prompt.

        callback_data shape: `tga:<request_id>:<option_index>`. The hook
        script (running as bux, blocking on a state file) polls
        /tmp/tg-approvals/<id>.json — we write the chosen index there,
        then strip the buttons off the original message and dismiss the
        loading spinner on the user's tap. Owner-only: a tap from anyone
        else gets an alert toast and no state file write.
        """
        try:
            data = cb.get("data") or ""
            if data.startswith("steer:"):
                self._handle_steer_callback(cb, data)
                return
            if data == "codex_login_retry":
                msg = cb.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                if not chat_id:
                    return
                sender = cb.get("from") or {}
                owner = _owner_for(chat_id, self.state)
                if owner and not _is_owner({"user_id": sender.get("id")}, owner):
                    self.call(
                        "answerCallbackQuery",
                        callback_query_id=cb["id"],
                        text="Only the box owner can retry Codex login.",
                        show_alert=True,
                    )
                    return
                self.call(
                    "answerCallbackQuery",
                    callback_query_id=cb["id"],
                    text="Retrying Codex login...",
                )
                self._start_login_provider(
                    "codex",
                    CODEX_AUTH_PROVIDER,
                    chat_id,
                    msg.get("message_id"),
                    int(msg.get("message_thread_id") or 0),
                    force=True,
                )
                return
            parts = (cb.get("data") or "").split(":")
            if len(parts) != 3 or parts[0] != "tga":
                return
            request_id, idx_str = parts[1], parts[2]
            try:
                option_index = int(idx_str)
            except ValueError:
                return
            sender = cb.get("from") or {}
            owner_id = self.state.get("owner", {}).get("user_id")
            if owner_id and str(sender.get("id")) != str(owner_id):
                self.call("answerCallbackQuery", callback_query_id=cb["id"],
                          text="Only the box owner can answer.", show_alert=True)
                return
            state_dir = Path("/tmp/tg-approvals")
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / f"{request_id}.json").write_text(json.dumps({
                "option_index": option_index,
                "user_id": sender.get("id"),
                "answered_at": time.time(),
            }))
            self.call("answerCallbackQuery", callback_query_id=cb["id"],
                      text=f"✅ option {option_index + 1}")
            msg = cb.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            mid = msg.get("message_id")
            if chat_id and mid:
                # Strip the buttons in place so a stale prompt can't be
                # double-tapped, and append a small footer telling the
                # other lane participants who answered what.
                self.call("editMessageReplyMarkup", chat_id=chat_id,
                          message_id=mid, reply_markup={"inline_keyboard": []})
                who = sender.get("username") or sender.get("id")
                self.call("sendMessage", chat_id=chat_id,
                          text=f"option {option_index + 1} chosen by @{who}",
                          reply_to_message_id=mid)
        except Exception:
            LOG.exception("callback_query handler failed")

    def _handle_steer_callback(self, cb: dict, data: str) -> None:
        """Process a Steer-button tap from a queue-ack bubble.

        callback_data shape: `steer:<lane_slug>:<job_id>`.

        Semantics: kill whatever's in_flight in the lane and promote the
        tapped job to the head of the queue, so the worker drains it
        next. The agent's session UUID / codex thread is unchanged, so
        the new prompt runs with the prior turn's tool history intact —
        only the cancelled prompt is replaced. Mirrors Claude Code's
        Esc-to-cancel-and-resend UX.

        Owner-only. A stale tap (job already in_flight, or already gone)
        gets a quiet toast + button-strip; we never kill an unrelated
        job by mistake.
        """
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        mid = msg.get("message_id")
        sender = cb.get("from") or {}
        parts = data.split(":", 2)
        if len(parts) != 3 or not chat_id:
            self.call("answerCallbackQuery", callback_query_id=cb["id"],
                      text="bad steer payload", show_alert=False)
            return
        slug, job_id = parts[1], parts[2]

        owner = _owner_for(int(chat_id), self.state)
        if not _is_owner({"user_id": sender.get("id")}, owner):
            self.call(
                "answerCallbackQuery",
                callback_query_id=cb["id"],
                text="Only the box owner can steer.",
                show_alert=True,
            )
            return

        moved = _lane_promote_to_front(slug, job_id)
        if moved is None:
            # Job is already in_flight or completed — strip the keyboard
            # so the bubble stops looking actionable, and toast quietly.
            if mid:
                try:
                    self.call("editMessageReplyMarkup", chat_id=chat_id,
                              message_id=mid,
                              reply_markup={"inline_keyboard": []})
                except Exception:
                    LOG.exception("strip stale steer keyboard failed")
            self.call(
                "answerCallbackQuery",
                callback_query_id=cb["id"],
                text="already running or finished",
            )
            return

        with _inflight_lock:
            proc = _inflight_procs.get(slug)
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    LOG.exception("steer kill of in-flight failed for %s", slug)

        if mid:
            try:
                self.call("editMessageReplyMarkup", chat_id=chat_id,
                          message_id=mid,
                          reply_markup={"inline_keyboard": []})
            except Exception:
                LOG.exception("steer keyboard strip failed")
        self.call(
            "answerCallbackQuery",
            callback_query_id=cb["id"],
            text="🚀 steered — running this next",
        )


def _consume_update_request_lanes() -> set["LaneKey"]:
    """Read + delete the one-shot file written by /update. Lanes listed here get
    a "back online" confirmation regardless of SHA delta or pending work."""
    try:
        text = UPDATE_REQUEST_LANES.read_text()
    except FileNotFoundError:
        return set()
    try:
        UPDATE_REQUEST_LANES.unlink()
    except FileNotFoundError:
        pass
    out: set[LaneKey] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        chat_str, _, thread_str = line.partition("\t")
        try:
            chat_id = int(chat_str)
        except ValueError:
            continue
        try:
            thread_id = int(thread_str) if thread_str else 0
        except ValueError:
            thread_id = 0
        out.add((chat_id, thread_id))
    return out


def _announce_online_if_new_sha(bot: "Bot") -> None:
    """Boot announcement, scoped to lanes that care about the restart.

    Two paths:

    Two-stage (busy lanes, fires on user-triggered restarts): a "🔄 restarting
    (sha=…)" message in each lane that had pending work when the restart hit,
    edited in place to "✅ fully ready (sha=…)" once that lane's leftover work
    has drained. Idle lanes get nothing — a restart is noise to a user whose
    conversation wasn't mid-task.

    One-shot (user-requested updates): the lane that ran /update gets a
    "✅ back online" confirmation unconditionally — even on idle lanes and
    even when the SHA didn't change (e.g. /update with no new commits).
    They explicitly asked for the restart, so they get a confirmation.

    Gate (suppresses the whole announce on non-user-triggered restarts):
    bux-tg gets restarted by plenty of things that aren't user-initiated —
    systemd flaps, long-poll backoff escapes, the post-update agent restart
    itself. We treat a restart as user-triggered if EITHER the SHA changed
    (someone manually pulled + restarted) OR a /update requester is recorded.
    Otherwise we stay silent across the board.
    """
    try:
        repo = "/opt/bux/repo"
        sha = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        if not sha:
            return
        try:
            last = LAST_ANNOUNCED_SHA.read_text().strip()
        except FileNotFoundError:
            last = ""
        sha_changed = sha != last
        requesters = _consume_update_request_lanes()
        if not sha_changed and not requesters:
            return
        branch = (
            subprocess.run(
                ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            or "?"
        )
        chats = load_allow()
        if not chats:
            return

        # Snapshot leftover work per lane BEFORE the worker drain has had
        # time to chew through it. We snapshot whenever the restart is
        # user-triggered (sha changed via out-of-band pull, OR a requester
        # is present from /update) — busy lanes need the two-stage 🔄→✅
        # treatment in either case so they know their queued work survives
        # the restart. The original SHA-only gate was too narrow: it meant
        # a /update on an already-current branch would silently skip
        # busy lanes AND fire a "✅ back online" to the requester before
        # their own pending work had drained.
        pending_by_lane = (
            _snapshot_pending_by_lane() if (sha_changed or requesters) else {}
        )

        msg_ids: dict[LaneKey, int] = {}
        all_pending_ids: set[str] = set()
        if pending_by_lane:
            ready_text = f"✅ fully ready (sha={sha}, branch={branch})"
            boot_text = f"🔄 restarting (sha={sha}, branch={branch})"
            for (chat_id, thread_id), ids in sorted(pending_by_lane.items()):
                # Respect the allow list — a stale lane for a chat that's no
                # longer bound shouldn't get a message it can't read anyway.
                if chat_id not in chats:
                    continue
                try:
                    mid = bot.send_returning_id(
                        chat_id=chat_id,
                        thread_id=thread_id or None,
                        text=boot_text,
                    )
                except Exception:
                    LOG.exception(
                        "online-announce send failed for chat=%s thread=%s",
                        chat_id,
                        thread_id,
                    )
                    continue
                if isinstance(mid, int):
                    msg_ids[(chat_id, thread_id)] = mid
                    all_pending_ids |= ids

        # User-requested update: send a single "back online" to each
        # requester lane that wasn't already covered by the two-stage
        # announce above (avoid duplicate ping for a busy lane that also
        # ran /update).
        back_text = f"✅ back online (sha={sha}, branch={branch})"
        for (chat_id, thread_id) in sorted(requesters):
            if chat_id not in chats:
                continue
            if (chat_id, thread_id) in msg_ids:
                continue
            try:
                bot.send(
                    chat_id=chat_id,
                    thread_id=thread_id or None,
                    text=back_text,
                )
            except Exception:
                LOG.exception(
                    "back-online send failed for chat=%s thread=%s",
                    chat_id,
                    thread_id,
                )

        LAST_ANNOUNCED_SHA.parent.mkdir(parents=True, exist_ok=True)
        LAST_ANNOUNCED_SHA.write_text(sha + "\n")

        if not msg_ids:
            return

        def _watch_drain() -> None:
            # Poll every couple of seconds until the snapshot is gone.
            # We watch by id, not by lane size: if a NEW user message
            # lands during the restart window, that doesn't delay the
            # "ready" flip — we only care about the leftover work.
            while True:
                time.sleep(2)
                with _lanes_lock:
                    still_alive = any(
                        j.get("id") in all_pending_ids
                        for jobs in _lanes.values()
                        for j in jobs
                    )
                if not still_alive:
                    break
            for (chat_id, thread_id), mid in msg_ids.items():
                try:
                    bot.edit(chat_id=chat_id, message_id=mid, text=ready_text)
                except Exception:
                    LOG.exception(
                        "ready-edit failed for chat=%s thread=%s",
                        chat_id,
                        thread_id,
                    )

        threading.Thread(
            target=_watch_drain,
            daemon=True,
            name="boot-drain-watcher",
        ).start()
    except Exception:
        LOG.exception("announce_online_if_new_sha failed")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    env = _read_kv(TG_ENV)
    token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN")
    setup_token = env.get("TG_SETUP_TOKEN") or os.environ.get("TG_SETUP_TOKEN", "")
    if not token:
        LOG.error("TG_BOT_TOKEN missing in %s", TG_ENV)
        return 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # Hydrate the on-disk lanes; in_flight rows from a previous crash are
    # dropped (we can't tell if the agent finished). Pending queued rows
    # survive across restarts.
    _lanes_init()
    bot = Bot(token, setup_token)
    # Idempotent: re-assert the default admin rights the bot needs whenever
    # it boots, so a fresh install (or a settings drift in BotFather) ends
    # up in a known good state without the operator having to remember.
    bot.assert_default_admin_rights()
    # Re-register the slash-command list with TG so typing `/` in chat
    # shows an up-to-date autocomplete tooltip. Idempotent; setMyCommands
    # overwrites whatever was there.
    bot.assert_my_commands()
    # Resume drain workers for any lane that still has queued work from
    # before the restart. Otherwise the "🔄 restarting → ✅ fully ready"
    # status flip in `_announce_online_if_new_sha` would never observe
    # progress: the snapshot would just sit there with no worker to drain
    # it until a new user message happened to land in that lane.
    resumed = _resume_pending_workers(bot._lane_drain)
    if resumed:
        LOG.info("resumed drain workers for %d lane(s) with pending jobs", resumed)
    # Announce *before* poll_loop so the user gets the "back online" ping
    # immediately on restart, not whenever the first long-poll completes.
    _announce_online_if_new_sha(bot)
    bot.poll_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
