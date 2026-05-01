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
  /etc/bux/tg-state.json         — {offset, agents: {lane_slug: 'claude'|'codex'}}
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
                return data
        except Exception:
            pass
    return {"offset": 0, "agents": {}}


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


def _session_uuid_for(key: LaneKey) -> str:
    """Return the per-lane claude/codex session UUID, persisting on first call.

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

    def _build_env(self, key: LaneKey, agent: str) -> dict[str, str]:
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
                self._run_codex(key, prompt, reply_to)
            else:
                self._run_claude(key, prompt, reply_to)
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
    ) -> None:
        """Stream a claude turn into the lane's TG topic.

        Uses --output-format=stream-json so each parent assistant text
        block lands as its own TG message bubble as it arrives. Sub-agent
        internal events (those carrying parent_tool_use_id) are silently
        dropped — the user only sees the orchestrator's voice.
        """
        chat_id, thread_id = key
        env = self._build_env(key, AGENT_CLAUDE)
        sid = _session_uuid_for(key)

        # --session-id is the create-or-resume flag: claude creates a session
        # with this UUID on first use and resumes it on subsequent calls. The
        # neighbor flag --resume is RESUME-ONLY (errors / opens the picker if
        # the session doesn't exist), which would break on the first message
        # of every fresh lane. Always use --session-id with our pinned UUID.
        cmd = ["sudo", "-u", "bux", "-H"] + [f"{k}={v}" for k, v in env.items() if v]
        cmd += [
            "/usr/bin/claude",
            "-p",
            "--session-id",
            sid,
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
            proc = subprocess.Popen(
                cmd,
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

        # No wall-clock cap: the agent can run as long as the user's task
        # needs. A genuinely-stuck claude pid sits forever until the user
        # kills it from ssh; that cost is local to one topic.

        any_text = False
        assert proc.stdout is not None
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

        if not any_text:
            # Stream produced nothing visible — fall back to a plain run so
            # the user gets *something*. Keeps the bot honest if claude
            # hiccuped on the streaming format.
            fb_cmd = ["sudo", "-u", "bux", "-H"] + [f"{k}={v}" for k, v in env.items() if v]
            fb_cmd += [
                "/usr/bin/claude",
                "-p",
                "--session-id",
                sid,
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

    def _run_codex(
        self,
        key: LaneKey,
        prompt: str,
        reply_to: int | None,
    ) -> None:
        """Stream a codex turn into the lane's TG topic.

        Uses `codex exec --json` (JSONL events). Forwards `item.completed`
        events of type `agent_message` as TG bubbles. Codex doesn't natively
        resume a thread by id from `exec`, so each message is independent —
        conversation continuity within the lane is best-effort (codex's own
        context window).
        """
        chat_id, thread_id = key
        env = self._build_env(key, AGENT_CODEX)

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
        cmd += [codex_bin, "exec", "--json", "--skip-git-repo-check", prompt]

        # stderr → file rather than DEVNULL so the no-output path can
        # surface the actual error message to the user (codex tends to
        # print friendly errors to stderr on auth / rate-limit issues).
        # Using a tempfile (not PIPE) avoids the 64 KB pipe deadlock.
        import tempfile

        stderr_buf = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                cmd,
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

        any_text = False
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                try:
                    ev = json.loads(line.strip() or "{}")
                except Exception:
                    continue
                et = ev.get("type") or ""
                # Codex JSONL events of interest:
                #   item.completed { item: { type: 'agent_message', text: '...' } }
                #   turn.completed (terminal)
                #   turn.failed (terminal, error)
                if et.startswith("item.") and et.endswith("completed"):
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

    def _bind_chat(self, chat_id: int) -> None:
        """Register chat_id, burn the setup_token, welcome the user."""
        add_allow(chat_id)
        burn_setup_token()
        self.setup_token = ""
        LOG.info("authorized chat_id=%s", chat_id)
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
        thread_id_raw = msg.get("message_thread_id")
        thread_id = thread_id_raw if isinstance(thread_id_raw, int) else 0
        text = (msg.get("text") or "").strip()
        caption = (msg.get("caption") or "").strip()
        if caption and not text:
            text = caption
        mid = msg.get("message_id")
        allow = load_allow()

        # Binding: first message wins. Topic-id is irrelevant — once the
        # parent chat is bound, every topic in it is automatically allowed.
        if chat_id not in allow:
            if not self.setup_token:
                LOG.info("dropping msg from chat_id=%s (already bound)", chat_id)
                return
            LOG.info("binding chat_id=%s (first-message wins)", chat_id)
            self._bind_chat(chat_id)
            return

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
                "parallel (cap 4 concurrent).\n\n"
                "Commands\n"
                "/agent claude|codex — switch this topic to a different agent\n"
                "/live — live-view URL of the active browser\n"
                "/queue — pending tasks in this topic\n"
                "/cancel — drop everything pending in this topic\n"
                "/cancel <id> — drop one pending task\n"
                "/schedules — list reminders / cron jobs",
                reply_to=mid,
                thread_id=thread_id,
            )
            return
        if cmd == "/whoami":
            self.send(
                chat_id,
                f"chat_id: {chat_id}\nthread_id: {thread_id}\nlane: `{slug}`\n"
                f"agent: `{_agent_for(key, self.state)}`",
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
        if not job_id:
            dropped = _drop_all_queued(slug)
            if dropped == 0:
                self.send(
                    chat_id,
                    "Nothing queued to cancel.",
                    reply_to=reply_to,
                    thread_id=thread_id,
                )
            else:
                self.send(
                    chat_id,
                    f"Cancelled {dropped} pending task(s). In-flight task continues.",
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
            self.send(
                chat_id,
                f"Task `{job_id}` is already running and can't be cancelled. "
                "It'll finish on its own.",
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
                try:
                    self.typing(chat_id, thread_id=key[1])
                    self.run_task(
                        key,
                        prompt,
                        reply_to=mid if isinstance(mid, int) else None,
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
    bot.poll_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
