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
     bound agent (claude default; `/agent codex` flips it).
  4. Stream-json events from claude come back as TG message bubbles in the
     lane's topic, with reaction emojis on the user's message (🤔→🎉/💔).
"""

from __future__ import annotations

import grp
import json
import logging
import os
import pwd
import re
import secrets
import signal
import subprocess
import sys
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

# Reaction emojis on the user's message. Telegram's free-tier reaction
# allowlist excludes ⏳/✅/⚠️/❌ — these are verified-allowed picks.
EMOJI_WORKING = "🤔"
EMOJI_DONE = "🎉"
EMOJI_ERROR = "💔"

# Recognised agents per lane. Values double as PATH binary names.
AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"
AGENTS = (AGENT_CLAUDE, AGENT_CODEX)
DEFAULT_AGENT = AGENT_CLAUDE


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
    """
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


def load_allow() -> set[int]:
    if not ALLOWED_FILE.exists():
        return set()
    return {int(x) for x in ALLOWED_FILE.read_text().split() if x.strip()}


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


def _enqueue(slug: str, job: dict, run_drain) -> int:
    """Push job onto its lane's queue. Returns new lane depth.
    Spawns a worker if the lane has none alive (caller passes the drain fn)."""
    with _lanes_lock:
        _lanes.setdefault(slug, []).append(job)
        _save_lanes_to_disk_locked()
        depth = len(_lanes[slug])
        if slug not in _lane_workers:
            t = threading.Thread(
                target=run_drain,
                args=(slug,),
                name=f"lane-{slug}",
                daemon=True,
            )
            _lane_workers[slug] = t
            t.start()
    return depth


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


AUTH_PROVIDERS: dict[str, _GhProvider] = {
    "gh": _GhProvider(),
    # Future: 'vercel': _VercelProvider(), 'npm': _NpmProvider(), etc.
}


class Bot:
    def __init__(self, token: str, setup_token: str) -> None:
        self.token = token
        self.setup_token = setup_token
        self.api = f"https://api.telegram.org/bot{token}"
        self.client = httpx.Client(timeout=POLL_TIMEOUT + 10)
        self.state = load_state()

    # ----- Telegram API plumbing -----

    def call(self, method: str, **params) -> dict:
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
    ) -> None:
        """Send a message into a chat (and optional forum topic).

        If `markdown=True`, runs the input through MarkdownV2 conversion and
        retries as plain text on the inevitable 400 (TG's escape rules are a
        minefield). Plain text path is the default for bot-authored strings
        so a stray `_` doesn't trip on its way out.

        `thread_id` routes the reply into a forum topic so a per-topic
        conversation stays scoped.
        """
        chunks = (
            _chunk_for_telegram(text, REPLY_MAX)
            if markdown
            else [text[i : i + REPLY_MAX] or " " for i in range(0, max(len(text), 1), REPLY_MAX)]
        )
        for chunk in chunks:
            if markdown:
                rendered = _to_tg_markdown_v2(chunk)
                resp = self.call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=rendered,
                    reply_to_message_id=reply_to,
                    message_thread_id=thread_id or None,
                    parse_mode="MarkdownV2",
                )
                if resp.get("ok") is False and resp.get("error_code") == 400:
                    LOG.info("MarkdownV2 rejected, falling back to plain text")
                    self.call(
                        "sendMessage",
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=reply_to,
                        message_thread_id=thread_id or None,
                    )
            else:
                self.call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=chunk,
                    reply_to_message_id=reply_to,
                    message_thread_id=thread_id or None,
                )

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
                self._run_codex(key, prompt, reply_to, sender=sender)
            else:
                self._run_claude(key, prompt, reply_to, sender=sender)
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
                    # Only forward parent-turn assistant events. Sub-agent
                    # internals carry parent_tool_use_id and stay hidden.
                    if et == "assistant" and not ev.get("parent_tool_use_id"):
                        content = (ev.get("message") or {}).get("content") or []
                        for block in content:
                            if not (isinstance(block, dict) and block.get("type") == "text"):
                                continue
                            text = (block.get("text") or "").strip()
                            if not text:
                                continue
                            self.send(
                                chat_id,
                                text,
                                reply_to=reply_to,
                                thread_id=thread_id,
                                markdown=True,
                            )
                            if not any_text:
                                any_text = True
                                self.react(chat_id, reply_to, EMOJI_DONE)
                    elif et == "result":
                        # Turn complete; stream is done.
                        break
            except Exception:
                LOG.exception("stream-json loop failed; falling back to plain run")

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
                    self.send(
                        chat_id,
                        out,
                        reply_to=reply_to,
                        thread_id=thread_id,
                        markdown=True,
                    )
                    self.react(
                        chat_id,
                        reply_to,
                        EMOJI_DONE if fb.returncode == 0 else EMOJI_ERROR,
                    )
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
                "• ChatGPT subscription: run `codex login` once as the bux user "
                "(`sudo -iu bux codex login` from ttyd or ssh) and follow the OAuth flow.\n"
                "• API key: drop `OPENAI_API_KEY=...` into `/home/bux/.secrets/openai.env`.\n\n"
                "Pick one — if both are set, codex silently uses the API key "
                "for billing (openai/codex#20099).\n\n"
                "Or `/agent claude` to switch back.",
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
        if existing_thread:
            # Resume the lane's existing codex thread so conversation context
            # carries across messages. `codex exec resume <id>` is the
            # documented form. If the thread id is invalid (codex pruned it,
            # disk corruption), codex errors and we surface the stderr.
            cmd += [
                codex_bin,
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
            cmd += [codex_bin, "exec", "--json", "--skip-git-repo-check", prompt]

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
                    #   turn.completed (terminal)
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
                        if item.get("type") == "agent_message":
                            text = (item.get("text") or "").strip()
                            if text:
                                self.send(
                                    chat_id,
                                    text,
                                    reply_to=reply_to,
                                    thread_id=thread_id,
                                    markdown=True,
                                )
                                if not any_text:
                                    any_text = True
                                    self.react(chat_id, reply_to, EMOJI_DONE)
                    elif et in ("turn.completed", "turn.failed"):
                        # `turn.failed` is the only true terminal-error signal.
                        # `error` events also appear during transient reconnects
                        # ("Reconnecting... 1/5") and aren't fatal — see below.
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

    def _bind_chat(self, chat_id: int, sender: dict | None = None) -> None:
        """Register chat_id, burn the setup_token, record the owner, welcome."""
        add_allow(chat_id)
        burn_setup_token()
        self.setup_token = ""
        if sender and sender.get("user_id"):
            _set_owner_for(chat_id, sender, self.state)
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
            "agent session. `/agent codex` per-topic to switch from claude.",
        )

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
            if not self.setup_token:
                LOG.info("dropping msg from chat_id=%s (already bound)", chat_id)
                return
            LOG.info("binding chat_id=%s (first-message wins)", chat_id)
            self._bind_chat(chat_id, sender=sender)
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

        cmd, arg = _parse_command(text)
        if cmd in ("/start", "/help"):
            self.send(
                chat_id,
                "Text me anything — I'll run it on your bux.\n\n"
                "Forum topics each get their own agent session and run in "
                "parallel — no concurrency cap, only the box's RAM gates it.\n\n"
                "Commands\n"
                "/agent claude|codex — switch this topic to a different agent\n"
                "/live — live-view URL of the active browser\n"
                "/queue — pending tasks in this topic\n"
                "/cancel — kill the running task + drop everything pending in this topic\n"
                "/cancel <id> — cancel one task (running or queued)\n"
                "/schedules — list reminders / cron jobs\n"
                "/login — auth status / connect a service (e.g. /login gh)\n"
                "/logout — disconnect a service (e.g. /logout gh)\n"
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
        if cmd == "/version":
            self._cmd_version(chat_id, mid, thread_id)
            return
        if cmd == "/update":
            self._cmd_update(chat_id, mid, thread_id, arg)
            return
        if cmd == "/login":
            self._cmd_login(chat_id, mid, thread_id, arg)
            return
        if cmd == "/logout":
            self._cmd_logout(chat_id, mid, thread_id, arg)
            return

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
        final_prompt = (attach_prefix + text).strip() or (
            "Look at the attached file and tell me what it is."
        )
        job = {
            "id": _new_job_id(),
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": mid,
            "prompt": final_prompt,
            "queued_at": time.time(),
            "status": "queued",
            "sender": sender,
        }
        self.react(chat_id, mid, EMOJI_WORKING)
        depth = _enqueue(slug, job, self._lane_drain)
        self.typing(chat_id, thread_id=thread_id)
        # Only ack when the user is actually queueing behind something —
        # the typing indicator + reaction emoji are enough for "I see you".
        if depth > 1:
            self.send(
                chat_id,
                f"🧠 queued (#{depth}) — id `{job['id']}`",
                reply_to=mid,
                thread_id=thread_id,
                markdown=True,
            )

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
            if dropped == 0 and not killed_in_flight:
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
                f"This topic is using `{current}`.\n\n`/agent claude` or `/agent codex` to switch.",
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
                if not isinstance(chat_id, int):
                    LOG.warning("lane %s job %s missing chat_id; skipping", slug, job_id)
                    with _lanes_lock:
                        _finish_locked(slug, job_id)
                    continue
                key: LaneKey = (chat_id, thread_id if isinstance(thread_id, int) else 0)
                sender = job.get("sender") if isinstance(job.get("sender"), dict) else None
                try:
                    self.typing(chat_id, thread_id=key[1])
                    self.run_task(
                        key,
                        prompt,
                        reply_to=mid if isinstance(mid, int) else None,
                        sender=sender,
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
    ) -> None:
        """`/login` — list providers + status. `/login <name>` — start flow.

        The actual login runs in a background thread because device-code
        flows block until the user authorizes (gh polls for ~15min). We
        don't want the bot's main poll loop stuck waiting; instead we
        send progress messages from the worker thread, all routed back
        into the same forum topic via thread_id.
        """
        name = arg.strip().lower()
        if not name:
            # List status of every registered provider.
            lines = ["*Auth status:*"]
            for pname, prov in AUTH_PROVIDERS.items():
                connected, status = prov.check()
                icon = "✓" if connected else "·"
                lines.append(f"{icon} `{pname}` — {status}")
            lines.append("")
            lines.append("Use `/login <name>` to connect (e.g. `/login gh`).")
            self.send(
                chat_id,
                "\n".join(lines),
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        prov = AUTH_PROVIDERS.get(name)
        if prov is None:
            known = ", ".join(AUTH_PROVIDERS.keys()) or "(none)"
            self.send(
                chat_id,
                f"Unknown provider `{name}`. Known: {known}.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return
        # If already connected, short-circuit. Saves the user a redundant
        # device-code dance and prevents accidentally rotating their token.
        connected, status = prov.check()
        if connected:
            self.send(
                chat_id,
                f"✓ `{name}` already connected ({status}). Use `/logout {name}` to disconnect first.",
                reply_to=reply_to,
                thread_id=thread_id,
                markdown=True,
            )
            return

        def _on_progress(text: str) -> None:
            self.send(chat_id, text, reply_to=reply_to, thread_id=thread_id)

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

    def _cmd_logout(
        self,
        chat_id: int,
        reply_to: int | None,
        thread_id: int,
        arg: str,
    ) -> None:
        """`/logout` — list providers + status. `/logout <name>` — disconnect."""
        name = arg.strip().lower()
        if not name:
            # Same listing as /login (helps the user see what's currently
            # logged in without remembering which command they want).
            self._cmd_login(chat_id, reply_to, thread_id, "")
            return
        prov = AUTH_PROVIDERS.get(name)
        if prov is None:
            known = ", ".join(AUTH_PROVIDERS.keys()) or "(none)"
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
                # update_ids on channel_post / callback_query / inline_query /
                # poll / chat_join_request / etc — we only ever consume
                # message + edited_message. Every other update type the bot
                # would silently drop after the round-trip.
                params: dict = {
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": ["message", "edited_message"],
                }
                if self.state.get("offset"):
                    params["offset"] = self.state["offset"] + 1
                data = self.call("getUpdates", **params)
                updates = data.get("result", [])
                if updates:
                    self.state["offset"] = max(u["update_id"] for u in updates)
                    save_state(self.state)
                for u in updates:
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


def _announce_online_if_new_sha(bot: "Bot") -> None:
    """Tell every bound chat "✓ bux online (sha=…)" — but only once per SHA.

    Why a marker file instead of always-announce: bux-tg gets restarted by
    plenty of things that aren't user-initiated updates — systemd flaps,
    long-poll backoff escapes, the post-update agent restart itself. A naive
    "always send on startup" would spam the chat every time the service
    blips. So we cache the last SHA we announced in
    /var/lib/bux/last-announced.sha; same SHA → silent restart, different
    SHA (or first ever boot) → one message.

    No-op if no chat is bound yet (fresh install pre-/start). Best-effort
    throughout — failure to announce must never block bot startup, since the
    announcement is courtesy and the bot is the recovery surface.
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
        if sha == last:
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
        text = f"✓ bux online (sha={sha}, branch={branch})"
        for chat_id in chats:
            try:
                bot.send(chat_id, text)
            except Exception:
                LOG.exception("online-announce send failed for chat %s", chat_id)
        # Write only after at least one send attempt, so a transient TG
        # outage doesn't permanently suppress the announcement.
        LAST_ANNOUNCED_SHA.parent.mkdir(parents=True, exist_ok=True)
        LAST_ANNOUNCED_SHA.write_text(sha + "\n")
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
    # dropped (we can't tell if the agent finished). The bot then starts the
    # poll loop and lazily spawns lane workers as messages arrive.
    _lanes_init()
    bot = Bot(token, setup_token)
    # Announce *before* poll_loop so the user gets the "back online" ping
    # immediately on restart, not whenever the first long-poll completes.
    _announce_online_if_new_sha(bot)
    bot.poll_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
