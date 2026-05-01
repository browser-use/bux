"""Telegram bot — SDK-backed long-lived session per chat (Option A).

This is the next-gen bot architecture. It replaces the per-message
`claude -p` subprocess pattern in `telegram_bot.py` with one long-lived
`ClaudeSDKClient` per chat_id. Each client holds an async streaming
session against the local `claude` CLI; messages flow in via
`client.query(text)` and tokens flow out via `client.receive_messages()`.

Why: the subprocess model can't do CLI-like behaviour. You can't queue
a follow-up while a turn is in flight, can't soft-cancel a running
turn, and you pay session-resume overhead on every message. This file
fixes that.

Capabilities the SDK gives us:
  - concurrent query mid-turn  → user can send a follow-up that gets
    appended to the same conversation while the previous turn is still
    streaming, no more "queued (#N)" ack
  - soft-cancel via interrupt() → /cancel actually stops the current turn
  - per-chat session continuity → claude's context is preserved across
    messages without re-launching the CLI

Auth: same first-message-wins binding flow as the legacy bot. Slash
commands, voice transcription, /live, /schedules, MarkdownV2 escaping,
and the ⏳→✅ reaction emojis are ported verbatim.

Persistence: chat_id → session_id is mirrored to /var/lib/bux/sessions.json
so SDK clients reconnect to the same session UUID after a service restart.

Crash resilience: if a ClaudeSDKClient dies mid-stream the chat is
torn down and lazily re-created on the next message, with exponential
backoff after repeated failures.

Concurrency cap: per-chat pending-write queue is bounded (MAX_PENDING)
so spam can't OOM the box.

NOTE: this file is parallel to telegram_bot.py on this PR. install.sh
flips bux-tg.service over to it; the legacy file is left untouched and
will be deleted in a follow-up after this code bakes in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    UserMessage,
)

LOG = logging.getLogger("bux-tg-sdk")

TG_ENV = Path("/etc/bux/tg.env")
BOX_ENV = Path("/etc/bux/env")
BROWSER_ENV = Path("/home/bux/.claude/browser.env")
OPENAI_ENV = Path("/home/bux/.secrets/openai.env")
ALLOWED_FILE = Path("/etc/bux/tg-allowed.txt")
STATE_FILE = Path("/etc/bux/tg-state.json")
# Per-chat session id mirror. Lets a fresh process re-bind to the same
# claude session_id after a restart so context persists across reboots.
SESSIONS_FILE = Path("/var/lib/bux/sessions.json")

POLL_TIMEOUT = 30
REPLY_MAX = 3500

# Telegram caps bot file downloads at 20 MB on the free tier.
TG_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# Per-chat bound on outstanding query() submissions. Above this we 429
# the user instead of letting them OOM the box. The SDK queues writes
# internally, so this is an additional shield, not the only one.
MAX_PENDING = 10

# Backoff cap for chat-client recreation after repeated crashes.
CLIENT_BACKOFF_MAX = 60.0


# ---------------------------------------------------------------------------
# MarkdownV2 helpers — copied verbatim from telegram_bot.py. See that file
# for the rationale; rendering rules don't change between bot versions.
# ---------------------------------------------------------------------------

_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_MDV2_ESCAPE = {c: "\\" + c for c in _MDV2_SPECIALS}


def _escape_mdv2_plain(s: str) -> str:
    return "".join(_MDV2_ESCAPE.get(c, c) for c in s)


def _escape_mdv2_code(s: str) -> str:
    return s.replace("\\", "\\\\").replace("`", "\\`")


def _to_tg_markdown_v2(text: str) -> str:
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


# ---------------------------------------------------------------------------
# Auth + binding (identical to telegram_bot.py — see comments there)
# ---------------------------------------------------------------------------


def load_allow() -> set[int]:
    if not ALLOWED_FILE.exists():
        return set()
    return {int(x) for x in ALLOWED_FILE.read_text().split() if x.strip()}


def _chmod_root_bux_640(path: Path) -> None:
    import grp

    bux_gid = grp.getgrnam("bux").gr_gid
    os.chown(path, 0, bux_gid)
    path.chmod(0o640)


def add_allow(chat_id: int) -> None:
    ids = load_allow() | {chat_id}
    ALLOWED_FILE.write_text("\n".join(str(i) for i in sorted(ids)))
    _chmod_root_bux_640(ALLOWED_FILE)


def burn_setup_token() -> None:
    if not TG_ENV.exists():
        return
    kept: list[str] = []
    for line in TG_ENV.read_text().splitlines():
        if line.strip().startswith("TG_SETUP_TOKEN="):
            continue
        kept.append(line)
    TG_ENV.write_text("\n".join(kept) + ("\n" if kept else ""))
    _chmod_root_bux_640(TG_ENV)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"offset": 0}


def save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s))
    try:
        STATE_FILE.chmod(0o600)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-chat session_id persistence
# ---------------------------------------------------------------------------


def _load_sessions() -> dict[str, str]:
    """Return {str(chat_id): session_uuid}. Survives bot restarts so the
    same Claude session is resumed for that chat."""
    if not SESSIONS_FILE.exists():
        return {}
    try:
        raw = SESSIONS_FILE.read_text()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if v}
    except Exception:
        LOG.exception("reading %s failed; starting clean", SESSIONS_FILE)
        return {}


def _save_sessions(mapping: dict[str, str]) -> None:
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSIONS_FILE.with_suffix(".tmp")
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
        os.write(fd, json.dumps(mapping).encode())
    finally:
        os.close(fd)
    tmp.replace(SESSIONS_FILE)
    try:
        SESSIONS_FILE.chmod(0o600)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Voice / audio / video_note helpers (ported from telegram_bot.py)
# ---------------------------------------------------------------------------


def _load_openai_key() -> str | None:
    if not OPENAI_ENV.exists():
        return None
    kv = _read_kv(OPENAI_ENV)
    key = kv.get("OPENAI_API_KEY") or ""
    return key or None


def _extract_media(msg: dict) -> tuple[str | None, str, int]:
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
# Per-chat ClaudeSDKClient holder
# ---------------------------------------------------------------------------


class ChatClient:
    """One ClaudeSDKClient + receiver task + outbox queue per chat_id.

    Lifecycle:
      ensure_started() — lazy connect; idempotent
      enqueue_query(text) — writes to client.query() with a small bound
      interrupt() — soft-cancel current turn
      shutdown() — disconnect + cancel receiver task

    The receiver task drains client.receive_messages() and pushes
    user-visible text into the bot's send loop.
    """

    def __init__(
        self,
        bot: "Bot",
        chat_id: int,
        session_id: str,
        forwarded_env: dict[str, str],
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.session_id = session_id
        self.forwarded_env = forwarded_env

        self.client: ClaudeSDKClient | None = None
        self.receiver: asyncio.Task[None] | None = None
        # Bounded so spam can't queue infinite writes. The SDK's own
        # write buffer is unbounded, so this is the only backpressure.
        self.pending: asyncio.Semaphore = asyncio.Semaphore(MAX_PENDING)
        self._lifecycle_lock = asyncio.Lock()
        self._consec_failures = 0
        self._last_failure_at = 0.0
        # Track the message_id of the user's most recent message so the
        # receiver can attach reply / reaction context to streamed text.
        self.current_reply_to: int | None = None
        self.current_message_id: int | None = None
        # Set when a turn produces visible text — used to swap the
        # ⏳ reaction → ✅ exactly once per turn.
        self._reacted_done = False

    async def ensure_started(self) -> None:
        async with self._lifecycle_lock:
            if self.client is not None and self.receiver and not self.receiver.done():
                return
            await self._spawn()

    async def _spawn(self) -> None:
        """(Re)create the SDK client and receiver. Backs off on repeat failures."""
        if self._consec_failures > 0:
            # Exponential-ish backoff capped at CLIENT_BACKOFF_MAX.
            delay = min(2 ** min(self._consec_failures, 6), CLIENT_BACKOFF_MAX)
            since = time.monotonic() - self._last_failure_at
            if since < delay:
                await asyncio.sleep(delay - since)

        # Resume an existing claude session if we have one on disk;
        # otherwise pin a fresh one with --session-id so we can resume
        # later. continue_conversation is intentionally not used —
        # session_id keeps each chat isolated even when multiple chats
        # share the box.
        opts_kwargs: dict = {
            "permission_mode": "bypassPermissions",
            "cwd": "/home/bux",
            "env": dict(self.forwarded_env),
            "cli_path": "/usr/bin/claude",
        }
        # The SDK refuses session_id + resume together, so pick one. If
        # we already have a session uuid we resume; otherwise we ask
        # the SDK to start one with that id so future restarts can
        # resume against the same uuid.
        if Path(f"/home/bux/.claude/projects/-home-bux/{self.session_id}.jsonl").exists():
            opts_kwargs["resume"] = self.session_id
        else:
            opts_kwargs["session_id"] = self.session_id

        options = ClaudeAgentOptions(**opts_kwargs)
        client = ClaudeSDKClient(options=options)
        try:
            await client.connect()
        except Exception:
            self._consec_failures += 1
            self._last_failure_at = time.monotonic()
            LOG.exception(
                "ClaudeSDKClient.connect failed for chat_id=%s (attempt %d)",
                self.chat_id,
                self._consec_failures,
            )
            raise
        self.client = client
        self._consec_failures = 0
        self.receiver = asyncio.create_task(
            self._receive_loop(), name=f"recv-{self.chat_id}"
        )
        LOG.info(
            "started ClaudeSDKClient for chat_id=%s session_id=%s",
            self.chat_id,
            self.session_id,
        )

    async def _receive_loop(self) -> None:
        """Drain client.receive_messages() into TG sends.

        Mirrors the legacy bot's filter: only forward parent-turn
        AssistantMessages (skip sub-agent internals via parent_tool_use_id),
        and only TextBlocks. ResultMessage = end of a turn — flip the ⏳→✅
        reaction.
        """
        assert self.client is not None
        try:
            async for msg in self.client.receive_messages():
                try:
                    await self._dispatch(msg)
                except Exception:
                    LOG.exception("receiver dispatch failed for chat_id=%s", self.chat_id)
        except Exception:
            LOG.exception(
                "receiver loop crashed for chat_id=%s; client will be respawned on next message",
                self.chat_id,
            )
            # Tear down so the next ensure_started() rebuilds.
            await self._teardown_client()

    async def _dispatch(self, msg) -> None:
        if isinstance(msg, AssistantMessage):
            if msg.parent_tool_use_id:
                # Sub-agent assistant text — silent.
                return
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text = (block.text or "").strip()
                    if not text:
                        continue
                    await self.bot.send_async(
                        self.chat_id,
                        text,
                        reply_to=self.current_reply_to,
                        markdown=True,
                    )
                    if not self._reacted_done and self.current_message_id:
                        await self.bot.react_async(
                            self.chat_id, self.current_message_id, "🎉"
                        )
                        self._reacted_done = True
        elif isinstance(msg, ResultMessage):
            # End of turn. If we never sent text and the result has a
            # body, surface it (rare — usually means the model emitted
            # only tool_use without final text).
            if not self._reacted_done and self.current_message_id:
                # Either there was no text (treat as silent done) or
                # the turn errored — show 💔 vs 🎉 accordingly.
                emoji = "💔" if msg.is_error else "🎉"
                await self.bot.react_async(
                    self.chat_id, self.current_message_id, emoji
                )
                self._reacted_done = True
            if msg.is_error and msg.result:
                await self.bot.send_async(
                    self.chat_id,
                    f"❌ {msg.result}",
                    reply_to=self.current_reply_to,
                )
        elif isinstance(msg, (UserMessage, SystemMessage)):
            return

    async def submit(self, text: str, message_id: int | None) -> bool:
        """Hand `text` to claude. Returns False if the per-chat queue is full.

        We don't await the response here — the receiver task handles
        output. This is what lets a follow-up message be queued mid-turn:
        we just call client.query() again and the SDK appends to the
        same conversation stream.
        """
        if self.pending.locked() and self.pending._value <= 0:
            return False
        if not self.pending.acquire_nowait_safe():
            return False

        await self.ensure_started()
        # Reset the "have we reacted yet" gate for this turn. A later
        # turn while a previous one is still streaming will overwrite
        # current_message_id — that's fine, the latest user message is
        # the one whose reaction we want to flip.
        self._reacted_done = False
        self.current_reply_to = message_id
        self.current_message_id = message_id
        try:
            assert self.client is not None
            await self.client.query(text)
        except Exception:
            self.pending.release()
            LOG.exception(
                "client.query failed for chat_id=%s; tearing down", self.chat_id
            )
            await self._teardown_client()
            raise
        # Release one slot — we don't track when "this" turn ends, so
        # we treat the queue as "concurrent in-flight writes" which is
        # cheap to bound and matches what we actually care about
        # (preventing spam OOM, not exact RPS).
        self.pending.release()
        return True

    async def interrupt(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.interrupt()
        except Exception:
            LOG.exception("interrupt failed for chat_id=%s", self.chat_id)

    async def _teardown_client(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                LOG.exception("disconnect failed for chat_id=%s", self.chat_id)
        self.client = None
        if self.receiver and not self.receiver.done():
            self.receiver.cancel()
        self.receiver = None

    async def shutdown(self) -> None:
        await self._teardown_client()


# Tiny shim — asyncio.Semaphore.acquire is awaitable, but acquire_nowait
# is private (._value). Wrap that into a non-awaiting check + acquire.
def _acquire_nowait_safe(self: asyncio.Semaphore) -> bool:
    if self._value <= 0:
        return False
    self._value -= 1
    return True


asyncio.Semaphore.acquire_nowait_safe = _acquire_nowait_safe  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


class Bot:
    def __init__(self, token: str, setup_token: str) -> None:
        self.token = token
        self.setup_token = setup_token
        self.api = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=POLL_TIMEOUT + 10)
        self.state = load_state()
        self.sessions: dict[str, str] = _load_sessions()
        self.chat_clients: dict[int, ChatClient] = {}

    # --- TG API ---------------------------------------------------------

    async def call(self, method: str, **params) -> dict:
        try:
            r = await self.client.post(
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

    async def send_async(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        markdown: bool = False,
    ) -> None:
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
                resp = await self.call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=rendered,
                    reply_to_message_id=reply_to,
                    parse_mode="MarkdownV2",
                )
                if resp.get("ok") is False and resp.get("error_code") == 400:
                    LOG.info("MarkdownV2 rejected, falling back to plain text")
                    await self.call(
                        "sendMessage",
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=reply_to,
                    )
            else:
                await self.call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=chunk,
                    reply_to_message_id=reply_to,
                )

    async def typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", chat_id=chat_id, action="typing")

    async def react_async(
        self, chat_id: int, message_id: int | None, emoji: str | None
    ) -> None:
        if not message_id:
            return
        reaction = [] if emoji is None else [{"type": "emoji", "emoji": emoji}]
        await self.call(
            "setMessageReaction",
            chat_id=chat_id,
            message_id=message_id,
            reaction=reaction,
        )

    # --- Chat client lifecycle -----------------------------------------

    def _get_or_alloc_session_id(self, chat_id: int) -> str:
        key = str(chat_id)
        sid = self.sessions.get(key)
        if sid and len(sid) == 36 and sid.count("-") == 4:
            return sid
        sid = str(uuid.uuid4())
        self.sessions[key] = sid
        try:
            _save_sessions(self.sessions)
        except Exception:
            LOG.exception("failed to persist sessions.json")
        LOG.info("allocated new session_id=%s for chat_id=%s", sid, chat_id)
        return sid

    def _forwarded_env(self) -> dict[str, str]:
        """Env passed to the claude CDP subprocess. Mirrors the legacy
        bot's `forwarded` dict so claude sees the same BU + browser env."""
        box_env = _read_kv(BOX_ENV)
        browser_env = _read_kv(BROWSER_ENV)
        env: dict[str, str] = {
            "HOME": "/home/bux",
            "USER": "bux",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        if box_env.get("BROWSER_USE_API_KEY"):
            env["BROWSER_USE_API_KEY"] = box_env["BROWSER_USE_API_KEY"]
        if box_env.get("BUX_PROFILE_ID"):
            env["BUX_PROFILE_ID"] = box_env["BUX_PROFILE_ID"]
            env["BU_PROFILE_ID"] = box_env["BUX_PROFILE_ID"]
        for k in ("BU_CDP_WS", "BU_BROWSER_ID", "BU_BROWSER_EXPIRES_AT"):
            if browser_env.get(k):
                env[k] = browser_env[k]
        return env

    def _chat_client(self, chat_id: int) -> ChatClient:
        cc = self.chat_clients.get(chat_id)
        if cc is not None:
            return cc
        sid = self._get_or_alloc_session_id(chat_id)
        cc = ChatClient(self, chat_id, sid, self._forwarded_env())
        self.chat_clients[chat_id] = cc
        return cc

    # --- Voice transcription -------------------------------------------

    async def _transcribe_media(
        self, file_id: str, filename: str
    ) -> tuple[str | None, str | None]:
        api_key = _load_openai_key()
        if not api_key:
            return (
                None,
                "❌ voice transcription unavailable — OpenAI key not configured on the box.",
            )
        gf = await self.call("getFile", file_id=file_id)
        if not gf.get("ok"):
            return None, "❌ Telegram getFile failed; try resending the audio."
        file_path = (gf.get("result") or {}).get("file_path")
        if not file_path:
            return None, "❌ Telegram returned no file_path; try resending."
        dl_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        try:
            r = await self.client.get(dl_url, timeout=60)
            r.raise_for_status()
            audio_bytes = r.content
        except Exception as e:
            LOG.warning("TG file download failed: %s", e)
            return None, f"❌ couldn’t download the audio from Telegram: {e}"
        try:
            async with httpx.AsyncClient(timeout=60) as wc:
                resp = await wc.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (filename, audio_bytes)},
                    data={"model": "whisper-1"},
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

    # --- Binding --------------------------------------------------------

    async def _bind_chat(self, chat_id: int) -> None:
        add_allow(chat_id)
        burn_setup_token()
        self.setup_token = ""
        LOG.info("authorized chat_id=%s", chat_id)
        await self.send_async(
            chat_id,
            "✓ Linked.\n\n"
            f"Chat id: {chat_id}\n\n"
            "🔒 This bot is now locked to this chat only. "
            "Every other chat is silently dropped — even if someone "
            "somehow discovers the bot handle.\n\n"
            "Text me anything and I'll run it on your bux.",
        )

    # --- Slash commands -------------------------------------------------

    async def _cmd_help(self, chat_id: int) -> None:
        await self.send_async(
            chat_id,
            "Text me anything — I'll run it on your bux.\n"
            "/live — live view URL of the active browser\n"
            "/queue — pending writes for this chat\n"
            "/cancel — soft-cancel the in-flight turn (interrupt)\n"
            "/schedules — list reminders / cron jobs (ask claude to cancel)\n"
            "/reset — drop this chat's claude session and start fresh",
        )

    async def _cmd_queue(self, chat_id: int, reply_to: int | None) -> None:
        cc = self.chat_clients.get(chat_id)
        if cc is None:
            await self.send_async(chat_id, "No active session.", reply_to=reply_to)
            return
        # ._value is the slots remaining; pending is MAX_PENDING - ._value.
        depth = MAX_PENDING - cc.pending._value
        await self.send_async(
            chat_id,
            f"Pending writes: {depth}/{MAX_PENDING}.\n"
            f"Session: {cc.session_id}",
            reply_to=reply_to,
        )

    async def _cmd_cancel(self, chat_id: int, reply_to: int | None) -> None:
        cc = self.chat_clients.get(chat_id)
        if cc is None or cc.client is None:
            await self.send_async(chat_id, "Nothing in flight.", reply_to=reply_to)
            return
        await cc.interrupt()
        await self.send_async(
            chat_id, "🛑 sent interrupt — current turn should stop.", reply_to=reply_to
        )

    async def _cmd_reset(self, chat_id: int, reply_to: int | None) -> None:
        """Drop and re-create this chat's claude session. Useful when
        the conversation has wandered into something the user wants
        to forget, or when the SDK client is wedged."""
        cc = self.chat_clients.pop(chat_id, None)
        if cc is not None:
            await cc.shutdown()
        # Drop session_id so the next message allocates a brand-new one.
        self.sessions.pop(str(chat_id), None)
        try:
            _save_sessions(self.sessions)
        except Exception:
            LOG.exception("failed to persist sessions.json after reset")
        await self.send_async(chat_id, "🔄 session reset.", reply_to=reply_to)

    async def _cmd_schedules(self, chat_id: int, reply_to: int | None) -> None:
        """Same as legacy bot — list `at` jobs and crontab. Read-only."""
        lines: list[str] = []

        def _atq() -> str:
            try:
                return subprocess.run(
                    ["sudo", "-u", "bux", "atq"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
            except Exception:
                LOG.exception("atq failed")
                return ""

        def _at_dump(job_id: str) -> str:
            try:
                return subprocess.run(
                    ["sudo", "-u", "bux", "at", "-c", job_id],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout
            except Exception:
                LOG.exception("at -c %s failed", job_id)
                return ""

        atq_out = await asyncio.to_thread(_atq)

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
            dump = await asyncio.to_thread(_at_dump, job_id)
            for ln in reversed([x for x in dump.splitlines() if x.strip()]):
                if ln.strip().startswith("}"):
                    continue
                body = ln.strip()
                break
            at_rows.append((job_id, fire_time, body))

        if at_rows:
            lines.append("🕒 *Pending reminders*")
            for job_id, fire_time, body in at_rows:
                preview = body if len(body) <= 70 else body[:67] + "…"
                lines.append(f"· `{job_id}` — {fire_time}\n  {preview}")

        def _crontab() -> str:
            try:
                return subprocess.run(
                    ["sudo", "-u", "bux", "crontab", "-l"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout
            except Exception:
                LOG.exception("crontab -l failed")
                return ""

        cron_out = await asyncio.to_thread(_crontab)
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
            await self.send_async(chat_id, "Nothing scheduled.", reply_to=reply_to)
            return
        lines.append("")
        lines.append('_To cancel: ask claude ("cancel the 9am reminder")._')
        await self.send_async(
            chat_id, "\n".join(lines), reply_to=reply_to, markdown=True
        )

    async def _live_url(self) -> str:
        box_env = _read_kv(BOX_ENV)
        browser_env = _read_kv(BROWSER_ENV)
        api_key = box_env.get("BROWSER_USE_API_KEY")
        browser_id = browser_env.get("BU_BROWSER_ID")
        if not api_key:
            return "❌ no BROWSER_USE_API_KEY on this box"
        if not browser_id:
            return "❌ no active browser yet — keeper may still be starting"
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"https://api.browser-use.com/api/v3/browsers/{browser_id}",
                    headers={"X-Browser-Use-API-Key": api_key},
                )
                r.raise_for_status()
                live = r.json().get("liveUrl")
                if not live:
                    return "❌ browser has no liveUrl (session may be stale)"
                return f"🖥 {live}"
        except Exception as e:
            return f"❌ live-url lookup failed: {e}"

    # --- Inbound message dispatch --------------------------------------

    async def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()
        mid = msg.get("message_id")
        allow = load_allow()

        # First-message-wins binding.
        if chat_id not in allow:
            if not self.setup_token:
                LOG.info("dropping msg from chat_id=%s (already bound)", chat_id)
                return
            LOG.info("binding chat_id=%s (first-message wins)", chat_id)
            await self._bind_chat(chat_id)
            return

        # Voice / audio / video_note → Whisper, then continue as text.
        file_id, filename, file_size = _extract_media(msg)
        if file_id and not text:
            if file_size and file_size > TG_MAX_DOWNLOAD_BYTES:
                await self.send_async(
                    chat_id,
                    f"❌ file is {file_size // (1024 * 1024)} MB — Telegram caps bot "
                    "downloads at 20 MB. Send a shorter clip.",
                    reply_to=mid,
                )
                return
            await self.typing(chat_id)
            await self.send_async(chat_id, "🎤 transcribing…", reply_to=mid)
            transcript, err = await self._transcribe_media(file_id, filename)
            if err is not None or not transcript:
                await self.send_async(chat_id, err or "❌ transcription failed.", reply_to=mid)
                return
            await self.send_async(chat_id, f"📝 {transcript}", reply_to=mid)
            text = transcript

        cmd, _arg = _parse_command(text)
        if cmd in ("/start", "/help"):
            await self._cmd_help(chat_id)
            return
        if cmd == "/whoami":
            await self.send_async(chat_id, f"chat_id: {chat_id}")
            return
        if cmd == "/live":
            await self.send_async(chat_id, await self._live_url(), reply_to=mid)
            return
        if cmd == "/queue":
            await self._cmd_queue(chat_id, mid)
            return
        if cmd == "/cancel":
            await self._cmd_cancel(chat_id, mid)
            return
        if cmd == "/reset":
            await self._cmd_reset(chat_id, mid)
            return
        if cmd in ("/schedules", "/schedule"):
            await self._cmd_schedules(chat_id, mid)
            return

        if not text:
            return

        # Real work — hand to the chat's ClaudeSDKClient.
        cc = self._chat_client(chat_id)
        await self.typing(chat_id)
        await self.react_async(chat_id, mid, "🤔")
        try:
            ok = await cc.submit(text, mid)
        except Exception as e:
            LOG.exception("submit failed for chat_id=%s", chat_id)
            await self.react_async(chat_id, mid, "💔")
            await self.send_async(chat_id, f"❌ task failed: {e}", reply_to=mid)
            return
        if not ok:
            await self.send_async(
                chat_id,
                f"⚠ too many in-flight requests ({MAX_PENDING}). Wait for the current "
                "turn to finish or `/cancel` it.",
                reply_to=mid,
            )

    # --- Poll loop ------------------------------------------------------

    async def poll_loop(self) -> None:
        LOG.info("bux-tg-sdk starting poll loop")
        while True:
            try:
                params = {"timeout": POLL_TIMEOUT}
                if self.state.get("offset"):
                    params["offset"] = self.state["offset"] + 1
                data = await self.call("getUpdates", **params)
                updates = data.get("result", [])
                if updates:
                    self.state["offset"] = max(u["update_id"] for u in updates)
                    save_state(self.state)
                for u in updates:
                    msg = u.get("message") or u.get("edited_message")
                    if not msg:
                        continue
                    # Don't await — fire each handle as a task so /live or
                    # /cancel from a second message can preempt a long
                    # claude turn that's still streaming on the first.
                    asyncio.create_task(self._handle_safe(msg))
            except httpx.HTTPError:
                LOG.exception("poll failed; sleep 5s")
                await asyncio.sleep(5)
            except Exception:
                LOG.exception("unexpected; sleep 5s")
                await asyncio.sleep(5)

    async def _handle_safe(self, msg: dict) -> None:
        try:
            await self.handle(msg)
        except Exception:
            LOG.exception("handle failed")

    async def shutdown(self) -> None:
        for cc in list(self.chat_clients.values()):
            try:
                await cc.shutdown()
            except Exception:
                LOG.exception("chat shutdown failed")
        try:
            await self.client.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------


async def amain() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    env = _read_kv(TG_ENV)
    token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN")
    setup_token = env.get("TG_SETUP_TOKEN") or os.environ.get("TG_SETUP_TOKEN", "")
    if not token:
        LOG.error("TG_BOT_TOKEN missing in %s", TG_ENV)
        return 1

    bot = Bot(token, setup_token)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _signal_handler(*_a) -> None:
        if not stop.done():
            stop.set_result(None)

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, _signal_handler)
        except NotImplementedError:
            signal.signal(s, lambda *_: _signal_handler())

    poll = asyncio.create_task(bot.poll_loop(), name="poll")
    try:
        await stop
    finally:
        poll.cancel()
        try:
            await poll
        except (asyncio.CancelledError, Exception):
            pass
        await bot.shutdown()
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
