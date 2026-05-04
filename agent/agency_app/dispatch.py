"""Right-swipe dispatch — fire the suggestion's draft_action into the user's TG lane.

V1 strategy: shell out to `claude --dangerously-skip-permissions -p '<prompt>' | tg-send`
inside a backgrounded `nohup bash -c …`. The TG_CHAT_ID / TG_THREAD_ID env vars
route tg-send back to the lane the user was last active in.

Importing telegram_bot.run_task in-process would conflict with the live bot's
state (single Bot instance, lane queues, session UUID files), so we keep the
boundary at the OS process level.

V2 will replace this with a per-task forum topic created via createForumTopic
+ status pings, plus mid-execution permission asks that round-trip back into
the swipe deck.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

TG_STATE = Path(os.environ.get("BUX_TG_STATE", "/etc/bux/tg-state.json"))


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


def dispatch_action(user_id: str, suggestion_title: str, draft_action: str) -> bool:
    """Background a claude turn that runs `draft_action`, piping output to tg-send.

    Returns True if the subprocess was spawned. The actual work runs detached
    so the API request returns immediately.
    """
    chat_id, thread_id = primary_chat_for(user_id)
    if chat_id is None:
        return False
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
    if thread_id:
        env["TG_THREAD_ID"] = str(thread_id)
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        return True
    except Exception:
        return False
