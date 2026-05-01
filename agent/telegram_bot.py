"""Telegram bot running on the box. User-owned — browser-use never touches messages.

Auth: first-message-wins binding. The first chat_id that messages the freshly
installed bot becomes the owner; every subsequent chat is silently dropped.
TG_SETUP_TOKEN is a one-shot guard: while it's set, the first message binds;
after bind it's wiped from disk so a breach/backup leak can't bind a new chat.

Env (from /etc/bux/tg.env):
  TG_BOT_TOKEN     — Telegram bot token from @BotFather
  TG_SETUP_TOKEN   — random secret, present until the first chat binds

State (on disk):
  /etc/bux/tg-allowed.txt  — newline-separated allowed chat_ids (mode 600)
  /etc/bux/tg-state.json   — {offset: <last-update_id>}                (mode 600)

Flow:
  1. Start → TG_BOT_TOKEN required; begin long-polling getUpdates.
  2. First message from any chat while TG_SETUP_TOKEN is present → bind.
     All subsequent messages from other chats are silently dropped.
  3. Once bound, each message dispatches to `claude -p --resume <uuid>` so the
     whole conversation shares memory. Serialized via /home/bux/.bux/claude.lock.
  4. Commands: /start, /help, /whoami, /live (browser live-view URL).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
POLL_TIMEOUT = 30
REPLY_MAX = 3500  # TG's limit is 4096; we chunk anyway

# Telegram Bot API caps file downloads at 20 MB on the free tier — getFile
# returns ok but the subsequent download URL 404s past that. Bail early with
# a friendly message instead of trying.
TG_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# Persisted message queue. Each item is a Job dict:
#   id, chat_id, message_id, prompt, queued_at, status ∈ {queued, in_flight}
# A single worker thread drains this FIFO so claude is never run concurrently
# from the bot side. (box-agent shares /home/bux/.bux/claude.lock with us as
# the inter-process gate, so the lockfile stays.)


# Telegram MarkdownV2 has a strict escape set: every one of these chars,
# anywhere outside an entity, must be backslash-escaped. Inside `code` and
# ```pre``` only ` and \ are special. https://core.telegram.org/bots/api#markdownv2-style
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
    import re as _re

    blocks: list[str] = []

    def _stash_block(m):
        lang = (m.group(1) or "").strip()
        body = _escape_mdv2_code(m.group(2))
        blocks.append(f"```{lang}\n{body}\n```")
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    text = _re.sub(r"```([^\n`]*)\n(.*?)```", _stash_block, text, flags=_re.DOTALL)

    codes: list[str] = []

    def _stash_code(m):
        codes.append("`" + _escape_mdv2_code(m.group(1)) + "`")
        return f"\x00CODE{len(codes) - 1}\x00"

    text = _re.sub(r"`([^`\n]+)`", _stash_code, text)

    pattern = _re.compile(
        r"\*\*(.+?)\*\*"
        r"|__(.+?)__"
        r"|(?<![*\w])\*([^*\n]+?)\*(?!\w)"
        r"|(?<![_\w])_([^_\n]+?)_(?!\w)"
        r"|\[([^\]\n]+)\]\(([^)\n]+)\)"
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

    rendered = _re.sub(r"\x00CODE(\d+)\x00", lambda m: codes[int(m.group(1))], rendered)
    rendered = _re.sub(
        r"\x00BLOCK(\d+)\x00", lambda m: blocks[int(m.group(1))], rendered
    )
    return rendered


def _chunk_for_telegram(text: str, max_len: int) -> list[str]:
    """Split on paragraph boundaries when possible so we don't slice
    mid-formatting (TG would 400 on that for MarkdownV2)."""
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

    Strips the optional `@botname` suffix so /cmd@botname behaves like
    /cmd (TG sends the suffix in group chats). Splits on any whitespace
    via `split(None, 1)` so /cancel<tab>arg or /cancel<nbsp>arg parses
    correctly — `partition(' ')` would only catch a regular space.

    Returns (None, '') for non-command messages.
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


def load_allow() -> set[int]:
    if not ALLOWED_FILE.exists():
        return set()
    return {int(x) for x in ALLOWED_FILE.read_text().split() if x.strip()}


def _chmod_root_bux_640(path: Path) -> None:
    """Set `path` to 0o640 root:bux. Raises on failure.

    Used for /etc/bux/tg.env and /etc/bux/tg-allowed.txt — both need to
    be readable by the bux user so the `tg-send` helper can post to TG
    from at/cron jobs. Fail loud rather than swallow: a silent chmod
    miss here leaves scheduled work broken with no breadcrumb back to
    the install / first-bind step.
    """
    import grp

    bux_gid = grp.getgrnam("bux").gr_gid
    os.chown(path, 0, bux_gid)
    path.chmod(0o640)


def add_allow(chat_id: int) -> None:
    ids = load_allow() | {chat_id}
    ALLOWED_FILE.write_text("\n".join(str(i) for i in sorted(ids)))
    # 0o640 root:bux — tg-send (running as bux) needs to read the bound
    # chat_id. The chat id isn't a secret; the bot token is.
    _chmod_root_bux_640(ALLOWED_FILE)


def burn_setup_token() -> None:
    """Remove TG_SETUP_TOKEN from /etc/bux/tg.env after first successful bind.

    Single-use: once a chat_id is bound, the setup token is useless and should
    not sit on disk. Anyone who later reads tg.env (breach, backup leak, etc.)
    can't bind a new chat.
    """
    if not TG_ENV.exists():
        return
    kept: list[str] = []
    for line in TG_ENV.read_text().splitlines():
        if line.strip().startswith("TG_SETUP_TOKEN="):
            continue
        kept.append(line)
    TG_ENV.write_text("\n".join(kept) + ("\n" if kept else ""))
    # 0o640 root:bux so tg-send (running as bux from at/cron) can read
    # the bot token. Fail loud rather than swallow — a silent chmod miss
    # here means scheduled work breaks at fire time. Worst-case exposure
    # is bounded: messages can only be delivered to the bound chat, not
    # arbitrary users.
    _chmod_root_bux_640(TG_ENV)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"offset": 0}


def save_state(s: dict) -> None:
    # 0600 — state lives in /etc/bux but there's no reason for non-root to read it.
    STATE_FILE.write_text(json.dumps(s))
    try:
        STATE_FILE.chmod(0o600)
    except Exception:
        pass


def _session_args() -> list[str]:
    """Claude CLI args that pin/reuse this box's claude session.

    First message ever: `--session-id <new>` creates the session and writes
    the uuid to /home/bux/.bux/session. Every subsequent message: `--resume
    <uuid>` picks up the same conversation — so the whole chat history stays
    coherent across messages.

    This runs as root (bux-tg.service). `/home/bux` is writable by the `bux`
    user, so any naive open()/chown() on paths under it can be hijacked via
    a planted symlink (e.g. symlink /home/bux/.bux/session → /etc/shadow,
    root overwrites shadow on first TG message). We use O_NOFOLLOW on open
    and lchown() for the chown to prevent that.
    """
    path = "/home/bux/.bux/session"
    dir_path = os.path.dirname(path)

    # Read existing session. O_NOFOLLOW → ELOOP if `path` is a symlink.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(fd, "r") as f:
                sid = f.read().strip()
        except Exception:
            os.close(fd)
            raise
        if len(sid) == 36 and sid.count("-") == 4:
            return ["--resume", sid]
    except FileNotFoundError:
        pass
    except OSError as e:
        LOG.warning("reading %s failed (%s); regenerating", path, e)

    # Ensure the directory exists and isn't itself a symlink.
    os.makedirs(dir_path, exist_ok=True)
    if os.path.islink(dir_path):
        raise RuntimeError(f"{dir_path} is a symlink; refusing to write session")

    sid = str(uuid.uuid4())
    # O_NOFOLLOW refuses to open through a pre-existing symlink at `path`.
    try:
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    except OSError as e:
        LOG.warning("creating %s failed (%s); session not persisted", path, e)
        return ["--session-id", sid]
    try:
        with os.fdopen(fd, "w") as f:
            f.write(sid)
    except Exception:
        os.close(fd)
        raise
    # lchown() never follows symlinks. Redundant after O_NOFOLLOW but cheap.
    try:
        import pwd

        bux = pwd.getpwnam("bux")
        os.lchown(path, bux.pw_uid, bux.pw_gid)
    except Exception:
        LOG.exception("chown %s failed", path)
    LOG.info("created new bux claude session_id=%s", sid)
    return ["--session-id", sid]


_CLAUDE_LOCK_PATH = "/home/bux/.bux/claude.lock"


def _open_lockfile() -> int:
    """Open the cross-process lockfile symlink-safely.

    TG bot runs as root, any other claude-invoking service (box-agent etc.)
    runs as bux. They share this lockfile to serialize claude invocations.
    If TG creates the file first, it would be owned root:root 0644 — which
    means bux can't open it for writing and hits Permission denied. Fix:
    create with mode 0664 AND chown to bux immediately so either side can
    open it later.

    /home/bux is bux-writable — a symlink at the lock path pointing at
    /etc/sudoers would otherwise let a compromised bux trick root into
    creating/touching arbitrary files. O_NOFOLLOW rejects symlinks at the
    final component.
    """
    dir_path = os.path.dirname(_CLAUDE_LOCK_PATH)
    os.makedirs(dir_path, exist_ok=True)
    if os.path.islink(dir_path):
        raise RuntimeError(f"{dir_path} is a symlink; refusing to open lock")
    # Try to create the file exclusively so we know for certain whether WE
    # made it (and therefore must hand it to bux) vs. opening one a peer
    # already owns. A racy existsthen-open check could otherwise either
    # miss the chown or unlink a file another process is actively flocking.
    created_by_us = False
    try:
        fd = os.open(
            _CLAUDE_LOCK_PATH,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
            0o664,
        )
        created_by_us = True
    except FileExistsError:
        fd = os.open(_CLAUDE_LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW)

    if created_by_us:
        # We just created it as root. Hand it to bux so peer services (which
        # run as bux) can open it too. If chown/chmod fails we must NOT
        # return silently — a root-owned lockfile is the original bug.
        # Don't unlink on failure: another caller in a concurrent process
        # could already be locking the same inode. Just close + re-raise.
        try:
            import pwd

            bux = pwd.getpwnam("bux")
            os.fchown(fd, bux.pw_uid, bux.pw_gid)
            os.fchmod(fd, 0o664)
        except Exception:
            LOG.exception("chown %s failed; leaving file in place", _CLAUDE_LOCK_PATH)
            os.close(fd)
            raise
    return fd


def _acquire_claude_lock() -> int:
    """Cross-process exclusive lock shared with box_agent.py's run_task.

    Returns the fd — caller must fcntl.LOCK_UN + os.close() it when done.
    Blocks until the lock is free.
    """
    fd = _open_lockfile()
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_claude_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Persistent FIFO queue for incoming TG messages. See cloud-side telegram_bot.py
# for the long-form rationale; same code mirrored here.
# ---------------------------------------------------------------------------


def _new_job_id() -> str:
    return secrets.token_hex(4)


def _load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        raw = QUEUE_FILE.read_text()
        data = json.loads(raw) if raw.strip() else []
        return data if isinstance(data, list) else []
    except Exception:
        LOG.exception("reading %s failed; treating as empty", QUEUE_FILE)
        return []


def _save_queue(jobs: list[dict]) -> None:
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
        os.write(fd, json.dumps(jobs).encode())
    finally:
        os.close(fd)
    tmp.replace(QUEUE_FILE)


_queue_lock = threading.Lock()
_queue_cv = threading.Condition(_queue_lock)
_queue: list[dict] = []


def _queue_init() -> None:
    global _queue
    with _queue_lock:
        _queue = _load_queue()
        dropped: list[dict] = []
        kept: list[dict] = []
        for j in _queue:
            if j.get("status") == "in_flight":
                dropped.append(j)
            else:
                kept.append(j)
        _queue = kept
        if dropped:
            LOG.warning(
                "dropping %d stale in_flight job(s) from previous run", len(dropped)
            )
        _save_queue(_queue)


def _queue_enqueue(job: dict) -> int:
    with _queue_cv:
        _queue.append(job)
        _save_queue(_queue)
        _queue_cv.notify_all()
        return len(_queue)


def _queue_pop_next() -> dict | None:
    with _queue_cv:
        while True:
            for j in _queue:
                if j.get("status") == "queued":
                    j["status"] = "in_flight"
                    _save_queue(_queue)
                    return j
            _queue_cv.wait(timeout=1.0)


def _queue_remove(job_id: str) -> dict | None:
    with _queue_cv:
        for i, j in enumerate(_queue):
            if j.get("id") == job_id:
                if j.get("status") != "queued":
                    return j
                _queue.pop(i)
                _save_queue(_queue)
                return j
        return None


def _queue_finish(job_id: str) -> None:
    with _queue_cv:
        for i, j in enumerate(_queue):
            if j.get("id") == job_id:
                _queue.pop(i)
                _save_queue(_queue)
                return


def _queue_snapshot(chat_id: int) -> list[dict]:
    with _queue_cv:
        return [dict(j) for j in _queue if j.get("chat_id") == chat_id]


def _try_acquire_claude_lock() -> int | None:
    """Non-blocking variant. Returns fd if acquired, None if already held."""
    fd = _open_lockfile()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def _load_openai_key() -> str | None:
    """Read OPENAI_API_KEY from /home/bux/.secrets/openai.env.

    The file is mode 600 owned by bux; we run as root so we can read it.
    Don't add the key to the systemd EnvironmentFile — keep it on disk so
    it can be rotated without a service restart. Returns None if missing
    or unset, so callers can degrade gracefully (reply with a friendly
    "voice transcription unavailable" instead of crashing the worker).
    """
    if not OPENAI_ENV.exists():
        return None
    kv = _read_kv(OPENAI_ENV)
    key = kv.get("OPENAI_API_KEY") or ""
    return key or None


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
        # Best-effort extension from mime_type or file_name.
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


class Bot:
    def __init__(self, token: str, setup_token: str) -> None:
        self.token = token
        self.setup_token = setup_token
        self.api = f"https://api.telegram.org/bot{token}"
        self.client = httpx.Client(timeout=POLL_TIMEOUT + 10)
        self.state = load_state()
        # Bounded worker pool so a message storm / reconnect replay can't
        # explode into unbounded thread growth. Claude itself is serialized
        # by flock, so most threads just sit in the queue blocked on the lock;
        # the cap only matters for bursty /live or /help traffic.
        self.workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bux-tg")

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
            return {
                "ok": False,
                "error_code": e.response.status_code,
                "description": body,
            }
        except Exception as e:
            LOG.warning("%s failed: %s", method, e)
            return {"ok": False}

    def send(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        markdown: bool = False,
    ) -> None:
        """Send a message, optionally with MarkdownV2 rendering. Falls
        back to plain text if TG rejects the escaping with HTTP 400."""
        chunks = (
            _chunk_for_telegram(text, REPLY_MAX)
            if markdown
            else [
                text[i : i + REPLY_MAX] or " "
                for i in range(0, max(len(text), 1), REPLY_MAX)
            ]
        )
        for chunk in chunks:
            if markdown:
                rendered = _to_tg_markdown_v2(chunk)
                resp = self.call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=rendered,
                    reply_to_message_id=reply_to,
                    parse_mode="MarkdownV2",
                )
                if resp.get("ok") is False and resp.get("error_code") == 400:
                    LOG.info("MarkdownV2 rejected, falling back to plain text")
                    self.call(
                        "sendMessage",
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=reply_to,
                    )
            else:
                self.call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=chunk,
                    reply_to_message_id=reply_to,
                )

    def typing(self, chat_id: int) -> None:
        self.call("sendChatAction", chat_id=chat_id, action="typing")

    # ------------------------------------------------------------------
    # Task dispatch — shell out to `claude -p "<text>"` on the box with
    # BU + profile envs forwarded. Uses --resume against a persistent per-box
    # session UUID so every message continues the same conversation.
    # ------------------------------------------------------------------
    def run_task(self, prompt: str) -> str:
        box_env = _read_kv(BOX_ENV)
        browser_env = _read_kv(BROWSER_ENV)

        # sudo strips the environment by default; `-E` only helps if sudoers
        # has `env_keep` entries for the specific vars. We don't want to
        # require a sudoers drop-in on OSS installs, so we pass each var
        # explicitly via `sudo VAR=value …` — that's always forwarded.
        forwarded: dict[str, str] = {
            "HOME": "/home/bux",
            "USER": "bux",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        if box_env.get("BROWSER_USE_API_KEY"):
            forwarded["BROWSER_USE_API_KEY"] = box_env["BROWSER_USE_API_KEY"]
        if box_env.get("BUX_PROFILE_ID"):
            forwarded["BUX_PROFILE_ID"] = box_env["BUX_PROFILE_ID"]
            forwarded["BU_PROFILE_ID"] = box_env["BUX_PROFILE_ID"]
        for k in ("BU_CDP_WS", "BU_BROWSER_ID", "BU_BROWSER_EXPIRES_AT"):
            if browser_env.get(k):
                forwarded[k] = browser_env[k]

        session_args = _session_args()

        # Cross-process flock shared with any other claude invoker on this
        # box. Acquire blocks, so concurrent messages queue cleanly.
        lock_fd = _acquire_claude_lock()
        try:
            try:
                # Run as bux. We are root (service runs as root so we can sudo).
                # `sudo VAR=val ...` sets env for the child without needing any
                # sudoers env_keep configuration.
                cmd = ["sudo", "-u", "bux", "-H"]
                cmd += [f"{k}={v}" for k, v in forwarded.items()]
                cmd += [
                    "/usr/bin/claude",
                    "-p",
                    *session_args,
                    "--output-format",
                    "text",
                    "--permission-mode",
                    "bypassPermissions",
                    prompt,
                ]
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    cwd="/home/bux",
                )
                out = (proc.stdout or "").strip()
                if not out and proc.returncode != 0:
                    # Bubble stderr only when claude actually failed — keeps
                    # normal replies clean, still surfaces diagnostics to TG
                    # when something broke. Set BUX_DEBUG=1 to always include.
                    err = (proc.stderr or "").strip()
                    return err or f"(no output; rc={proc.returncode})"
                if os.environ.get("BUX_DEBUG") and proc.stderr:
                    out = f"{out}\n\n--stderr--\n{proc.stderr.strip()}"
                return out or "(no output)"
            except subprocess.TimeoutExpired:
                return "⏱ Timed out after 30 min."
            except Exception as e:
                return f"❌ task failed: {e}"
        finally:
            _release_claude_lock(lock_fd)

    # ------------------------------------------------------------------
    def _transcribe_media(self, file_id: str, filename: str) -> tuple[str | None, str | None]:
        """Resolve a TG file_id, download the bytes, send to Whisper.

        Returns (transcript, error). Exactly one is non-None.
            - (text, None)       → transcription succeeded
            - (None, "msg")      → user-facing error message ready to send
        """
        api_key = _load_openai_key()
        if not api_key:
            return (
                None,
                "❌ voice transcription unavailable — OpenAI key not configured on the box.",
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
            return None, f"❌ couldn’t download the audio from Telegram: {e}"

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

    # ------------------------------------------------------------------
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
            "🔒 This bot is now locked to this chat only. "
            "Every other chat is silently dropped — even if someone "
            "somehow discovers the bot handle.\n\n"
            "Text me anything and I'll run it on your bux.",
        )

    def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        # Treat a caption attached to a media message as the prompt — that
        # way the user can dictate "summarize this PDF" alongside the file
        # and we skip transcription. Plain text messages are unaffected.
        text = (msg.get("text") or msg.get("caption") or "").strip()
        mid = msg.get("message_id")
        allow = load_allow()

        # Binding path — first-come-first-served.
        #
        # The bot was created seconds ago with a randomized, unenumerable
        # username (`bux_<8hex>_bot`, 62^8 ≈ 2×10^14 search space). Only the
        # user's local skill knows the name. So the first chat to message
        # this bot is, by construction, the owner.
        #
        # Once we have an allow-list entry, setup_token is burned and every
        # other chat_id is silently dropped forever.
        if chat_id not in allow:
            if not self.setup_token:
                # Already bound somewhere else. Strangers get nothing.
                LOG.info("dropping msg from chat_id=%s (already bound)", chat_id)
                return
            # First message from anyone while token still active → bind.
            LOG.info("binding chat_id=%s (first-message wins)", chat_id)
            self._bind_chat(chat_id)
            return

        # Voice / audio / video_note: download from TG, transcribe with
        # Whisper, then fall through to the normal text-message pipeline
        # as if the user had typed the transcript. If the message also
        # carries a caption we already adopted it as `text` above and
        # skip transcription entirely.
        file_id, filename, file_size = _extract_media(msg)
        if file_id and not text:
            if file_size and file_size > TG_MAX_DOWNLOAD_BYTES:
                self.send(
                    chat_id,
                    f"❌ file is {file_size // (1024 * 1024)} MB — Telegram caps bot "
                    "downloads at 20 MB. Send a shorter clip.",
                    reply_to=mid,
                )
                return
            # Quick ack so the user knows we're working — Whisper can take
            # 5-10s on a 30s clip and silence feels like the bot is broken.
            self.typing(chat_id)
            self.send(chat_id, "🎤 transcribing…", reply_to=mid)
            transcript, err = self._transcribe_media(file_id, filename)
            if err is not None or not transcript:
                self.send(chat_id, err or "❌ transcription failed.", reply_to=mid)
                return
            # Show the user what we heard, then fall through to the normal
            # pipeline using the transcript as the "typed" text.
            self.send(chat_id, f"📝 {transcript}", reply_to=mid)
            text = transcript

        # Commands. TG sends `/cmd@botname` in group chats so users can
        # disambiguate when multiple bots are present — strip the suffix
        # before matching so the bot still works if someone ever drops it
        # into a group. Today the binding flow guarantees a 1:1 chat, so
        # in practice this is just defense in depth.
        cmd, arg = _parse_command(text)
        if cmd in ("/start", "/help"):
            self.send(
                chat_id,
                "Text me anything — I'll run it on your bux.\n"
                "/live — live view URL of the active browser\n"
                "/queue — see pending tasks\n"
                "/cancel — drop everything pending\n"
                "/cancel <id> — drop one pending task\n"
                "/schedules — list reminders / cron jobs (ask claude to cancel)",
            )
            return
        if cmd == "/whoami":
            self.send(chat_id, f"chat_id: {chat_id}")
            return
        if cmd == "/live":
            self.send(chat_id, self._live_url(), reply_to=mid)
            return
        if cmd == "/queue":
            self._cmd_queue(chat_id, mid)
            return
        if cmd == "/cancel":
            self._cmd_cancel(chat_id, mid, arg)
            return
        if cmd in ("/schedules", "/schedule"):
            self._cmd_schedules(chat_id, mid)
            return

        # Enqueue and acknowledge. The dedicated worker thread does the
        # claude run and sends the result reply when it's done.
        job = {
            "id": _new_job_id(),
            "chat_id": chat_id,
            "message_id": mid,
            "prompt": text,
            "queued_at": time.time(),
            "status": "queued",
        }
        depth = _queue_enqueue(job)
        # Don't send "on it…" when the queue is empty — TG's typing
        # indicator already says "I see you, working on it". Only send
        # a visible ack when actually queueing.
        self.typing(chat_id)
        if depth > 1:
            self.send(
                chat_id,
                f"🧠 queued (#{depth}) — id `{job['id']}`",
                reply_to=mid,
                markdown=True,
            )

    def _cmd_queue(self, chat_id: int, reply_to: int | None) -> None:
        jobs = _queue_snapshot(chat_id)
        if not jobs:
            self.send(chat_id, "Queue is empty.", reply_to=reply_to)
            return
        lines = ["Pending:"]
        for j in jobs:
            marker = "▶" if j.get("status") == "in_flight" else "·"
            preview = (
                (j.get("prompt") or "").strip().splitlines()[0]
                if j.get("prompt")
                else ""
            )
            if len(preview) > 60:
                preview = preview[:57] + "…"
            lines.append(f"{marker} `{j['id']}` — {preview}")
        self.send(chat_id, "\n".join(lines), reply_to=reply_to, markdown=True)

    def _cmd_cancel(self, chat_id: int, reply_to: int | None, job_id: str) -> None:
        if not job_id:
            with _queue_cv:
                before = len(_queue)
                _queue[:] = [
                    j
                    for j in _queue
                    if j.get("status") != "queued" or j.get("chat_id") != chat_id
                ]
                dropped = before - len(_queue)
                _save_queue(_queue)
            if dropped == 0:
                self.send(chat_id, "Nothing queued to cancel.", reply_to=reply_to)
            else:
                self.send(
                    chat_id,
                    f"Cancelled {dropped} pending task(s). In-flight task continues.",
                    reply_to=reply_to,
                )
            return
        removed = _queue_remove(job_id)
        if removed is None or removed.get("chat_id") != chat_id:
            self.send(
                chat_id,
                f"No pending task with id `{job_id}`.",
                reply_to=reply_to,
                markdown=True,
            )
        elif removed.get("status") == "in_flight":
            self.send(
                chat_id,
                f"Task `{job_id}` is already running and can't be cancelled. It'll finish on its own.",
                reply_to=reply_to,
                markdown=True,
            )
        else:
            self.send(
                chat_id, f"Cancelled task `{job_id}`.", reply_to=reply_to, markdown=True
            )

    def _cmd_schedules(self, chat_id: int, reply_to: int | None) -> None:
        """List the bux user's pending `at` jobs and crontab.

        Read-only. Cancellation is intentionally NOT a bot command — users
        ask claude (\"cancel that 9am reminder\") which has the context to
        map a fuzzy description to a job id.
        """
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
            fire_time = (
                " ".join(parts[1:-2]) if len(parts) >= 4 else " ".join(parts[1:])
            )
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
            ln
            for ln in cron_out.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
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
            self.send(chat_id, "Nothing scheduled.", reply_to=reply_to)
            return
        lines.append("")
        lines.append('_To cancel: ask claude ("cancel the 9am reminder")._')
        self.send(chat_id, "\n".join(lines), reply_to=reply_to, markdown=True)

    def queue_worker(self) -> None:
        """Single drain loop. Pops one job, runs claude, replies."""
        LOG.info("bux-tg queue worker starting")
        while True:
            job = _queue_pop_next()
            if job is None:
                return
            # Be defensive about a malformed row (manual edit, future schema
            # change). Skip and move on rather than killing the worker.
            chat_id_raw = job.get("chat_id")
            mid_raw = job.get("message_id")
            job_id = str(job.get("id") or "?")
            if not isinstance(chat_id_raw, int):
                LOG.warning("queue job %s missing chat_id; skipping", job_id)
                _queue_finish(job_id)
                continue
            chat_id: int = chat_id_raw
            mid: int | None = mid_raw if isinstance(mid_raw, int) else None
            prompt = str(job.get("prompt") or "")
            try:
                self.typing(chat_id)
                result = self.run_task(prompt)
                self.send(chat_id, result, reply_to=mid, markdown=True)
            except Exception as e:
                LOG.exception("queue job %s failed", job_id)
                try:
                    self.send(chat_id, f"❌ task failed: {e}", reply_to=mid)
                except Exception:
                    LOG.exception("also failed to send error reply")
            finally:
                _queue_finish(job_id)

    # ------------------------------------------------------------------
    def _live_url(self) -> str:
        """Return the live-view URL of the box's current browser session."""
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

    def _handle_in_thread(self, msg: dict) -> None:
        """Run handle() off the poll loop so claude invocations don't block
        /live, /help, or the next `getUpdates`. Claude itself is serialized
        via the flock inside run_task, so multiple concurrent threads queue
        cleanly and the second one can report "queued" to the user."""
        try:
            self.handle(msg)
        except Exception:
            LOG.exception("handle failed")

    def poll_loop(self) -> None:
        LOG.info("bux-tg starting poll loop")
        while True:
            try:
                params = {"timeout": POLL_TIMEOUT}
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
                    self.workers.submit(self._handle_in_thread, msg)
            except httpx.HTTPError:
                LOG.exception("poll failed; sleep 5s")
                time.sleep(5)
            except Exception:
                LOG.exception("unexpected; sleep 5s")
                time.sleep(5)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    env = _read_kv(TG_ENV)
    token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN")
    setup_token = env.get("TG_SETUP_TOKEN") or os.environ.get("TG_SETUP_TOKEN", "")
    if not token:
        LOG.error("TG_BOT_TOKEN missing in %s", TG_ENV)
        return 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # Hydrate the on-disk queue (drops stale in_flight rows from a previous
    # crash) and spin up the single drain worker before the poll loop.
    _queue_init()
    bot = Bot(token, setup_token)
    threading.Thread(target=bot.queue_worker, name="bux-tg-queue", daemon=True).start()
    bot.poll_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
