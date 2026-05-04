"""Right-swipe dispatch — fire the suggestion's draft_action into the user's TG lane.

V2: try to createForumTopic on the bound supergroup so the dispatched task gets
its own forum topic. If the bound chat is not a forum, fall back to the V1
behavior (post into the user's primary topic / chat root).

V1 strategy preserved as fallback: shell out to
`claude --dangerously-skip-permissions -p '<prompt>' | tg-send` inside a
backgrounded `nohup bash -c …`. The TG_CHAT_ID / TG_THREAD_ID env vars
route tg-send back to the lane the user was last active in.

Importing telegram_bot.run_task in-process would conflict with the live bot's
state (single Bot instance, lane queues, session UUID files), so we keep the
boundary at the OS process level.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import urllib.request
from pathlib import Path

LOG = logging.getLogger("agency.dispatch")

TG_STATE = Path(os.environ.get("BUX_TG_STATE", "/etc/bux/tg-state.json"))
TG_ENV = Path(os.environ.get("BUX_TG_ENV", "/etc/bux/tg.env"))


def primary_chat_for(user_id: str) -> tuple[int | None, int | None]:
    """Pick a (chat_id, thread_id) to route this user's dispatched task into.

    Strategy: scan tg-state.json `owners` for chats where this user is owner,
    return the most recently bound one (no thread). The bot itself is the
    source of truth for lane state, so we just borrow its ledger.
    """
    try:
        state = json.loads(TG_STATE.read_text())
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return None, None
    candidates: list[tuple[int, int]] = []  # (bound_at, chat_id)
    for chat_str, owner in (state.get("owners") or {}).items():
        if not isinstance(owner, dict):
            continue
        if str(owner.get("user_id")) != str(user_id):
            continue
        try:
            chat_id = int(chat_str)
        except (TypeError, ValueError):
            continue
        bound_at = int(owner.get("bound_at") or 0)
        candidates.append((bound_at, chat_id))
    if not candidates:
        return None, None
    candidates.sort(reverse=True)
    return candidates[0][1], None


def _bot_token() -> str | None:
    tok = os.environ.get("TG_BOT_TOKEN")
    if tok:
        return tok
    try:
        for line in TG_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            if k.strip() == "TG_BOT_TOKEN":
                return v.strip().strip('"').strip("'")
    except (FileNotFoundError, PermissionError):
        return None
    return None


def _tg_api(token: str, method: str, payload: dict | None = None) -> dict | None:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if payload is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        if not body.get("ok"):
            LOG.info("tg %s not ok: %s", method, body)
            return None
        return body.get("result") or {}
    except Exception as e:
        LOG.info("tg %s failed: %s", method, e)
        return None


def _is_forum_chat(chat_id: int, token: str) -> bool:
    res = _tg_api(token, f"getChat?chat_id={chat_id}")
    return bool(res and res.get("is_forum"))


def create_forum_topic(chat_id: int, title: str) -> int | None:
    """Create a forum topic on chat_id; return its message_thread_id or None.

    Returns None if the bot token is missing, the chat is not a forum, or the
    Telegram API call fails. Caller is expected to fall back to the primary
    topic in that case.
    """
    token = _bot_token()
    if not token:
        return None
    if not _is_forum_chat(chat_id, token):
        LOG.info("chat %s is not a forum; falling back to primary topic", chat_id)
        return None
    name = (title or "task").strip()[:30] or "task"
    res = _tg_api(token, "createForumTopic", {"chat_id": chat_id, "name": name})
    if not res:
        return None
    thread_id = res.get("message_thread_id")
    return int(thread_id) if thread_id else None


def dispatch_action(
    user_id: str, suggestion_title: str, draft_action: str
) -> tuple[bool, int | None]:
    """Background a claude turn that runs `draft_action`, piping output to tg-send.

    Returns (dispatched, topic_id). topic_id is the per-task forum topic created
    via createForumTopic; None when the bound chat is not a forum (V1 fallback).
    """
    chat_id, thread_id = primary_chat_for(user_id)
    if chat_id is None:
        return False, None
    topic_id = create_forum_topic(chat_id, suggestion_title)
    effective_thread = topic_id if topic_id is not None else thread_id
    prompt = (
        f"User accepted this swipe-deck suggestion: \"{suggestion_title}\".\n\n"
        f"Action to perform:\n{draft_action}\n\n"
        "Run the action end-to-end. If you hit a permission boundary, stop and "
        "explain — the user will see the message in this topic."
    )
    inner = (
        "claude --dangerously-skip-permissions -p "
        f"{shlex.quote(prompt)} | tg-send"
    )
    cmd = ["nohup", "bash", "-c", inner]
    env = os.environ.copy()
    env["TG_CHAT_ID"] = str(chat_id)
    if effective_thread:
        env["TG_THREAD_ID"] = str(effective_thread)
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        return True, topic_id
    except Exception:
        return False, topic_id
