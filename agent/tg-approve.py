#!/usr/bin/env python3
"""tg-approve — bridge Claude's AskUserQuestion to Telegram inline buttons.

Wired in as a `PreToolUse` hook with matcher = "AskUserQuestion". When
claude calls the tool to ask the user a clarifying question with N
choices, this script intercepts the tool call, posts the question with
one inline-keyboard button per option to the same TG lane, blocks until
the user taps, then DENIES the tool call with the selected option text
in `permissionDecisionReason` — claude reads that as the answer.

We deliberately do NOT intercept Bash/Edit/Write/etc. The bot runs claude
with `--dangerously-skip-permissions`, so there are no Allow/Deny gates
to surface anyway. Only the deliberate "agent stops to ask" moments need
a TG bridge.

Routing relies on TG_CHAT_ID / TG_THREAD_ID being in env (the bot exports
both per agent-subprocess invocation). TG_BOT_TOKEN is read from
/etc/bux/tg.env (mode 640 root:bux, the bux user can read it).

If no answer comes in TG_APPROVE_TIMEOUT seconds (default 600), the
script falls through with `permissionDecision: "ask"` so claude's normal
flow runs — better than blocking on a question the user never saw.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib import request as _req

STATE_DIR = Path("/tmp/tg-approvals")
TIMEOUT_SEC = int(os.environ.get("TG_APPROVE_TIMEOUT", "600"))
POLL_SEC = 0.5
TG_API = "https://api.telegram.org/bot{token}/{method}"


def _read_bot_token() -> str:
    """Pull TG_BOT_TOKEN from /etc/bux/tg.env (KEY=VAL lines)."""
    for line in Path("/etc/bux/tg.env").read_text().splitlines():
        if line.startswith("TG_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("TG_BOT_TOKEN not found in /etc/bux/tg.env")


def _tg_call(token: str, method: str, payload: dict) -> dict:
    """Minimal POST helper — stdlib only so the script has zero deps."""
    body = json.dumps(payload).encode()
    req = _req.Request(
        TG_API.format(token=token, method=method),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _req.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _extract_question_and_options(tool_input: dict) -> tuple[str, list[str]]:
    """Pull (question, [option_text…]) out of the AskUserQuestion args.

    Defensive about field names — Anthropic has shifted the schema across
    versions. Tries `question` / `questionText` / `prompt` for the question
    and `options` / `multipleChoiceOptions` / `choices` for the list.
    Each option may be a bare string or a dict with `label` / `text` /
    `value` / `name`.
    """
    question = (
        tool_input.get("question")
        or tool_input.get("questionText")
        or tool_input.get("prompt")
        or "(no question text)"
    )
    raw = (
        tool_input.get("options")
        or tool_input.get("multipleChoiceOptions")
        or tool_input.get("choices")
        or []
    )
    options: list[str] = []
    for o in raw:
        if isinstance(o, str):
            options.append(o)
        elif isinstance(o, dict):
            options.append(
                o.get("label") or o.get("text") or o.get("value")
                or o.get("name") or json.dumps(o)
            )
    return str(question), options


def _post_question(token: str, text: str, options: list[str], request_id: str) -> dict:
    """Send the question with one button per option. Two-column grid keeps
    it phone-friendly when there are 4+ choices."""
    chat_id = os.environ.get("TG_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TG_CHAT_ID not in env — bot didn't export it")
    thread_id = os.environ.get("TG_THREAD_ID")
    # Telegram caps callback_data at 64 bytes; we encode just the index, not
    # the option text. Bridge resolves index → text from the state file.
    buttons = [
        {"text": f"{i + 1}. {opt[:50]}", "callback_data": f"tga:{request_id}:{i}"}
        for i, opt in enumerate(options)
    ]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    payload: dict = {
        "chat_id": int(chat_id),
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": keyboard},
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    return _tg_call(token, "sendMessage", payload)


def _wait_for_choice(request_id: str, options: list[str]) -> str | None:
    """Block on /tmp/tg-approvals/<id>.json until the bot writes a choice
    (or TIMEOUT_SEC elapses). Returns the chosen option text, or None on
    timeout — caller then falls through to claude's normal flow."""
    state_file = STATE_DIR / f"{request_id}.json"
    deadline = time.monotonic() + TIMEOUT_SEC
    while time.monotonic() < deadline:
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                state_file.unlink(missing_ok=True)
                idx = int(data.get("option_index", -1))
                if 0 <= idx < len(options):
                    return options[idx]
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        time.sleep(POLL_SEC)
    return None


def _emit(decision: str, reason: str = "") -> None:
    """Hook output schema for PreToolUse. `decision` is allow / deny / ask."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        hook_input = {}

    # Only intercept AskUserQuestion. Everything else falls through to
    # claude's normal flow (which, with --dangerously-skip-permissions, is
    # just "run the tool"). Defending against accidental wide matchers.
    if hook_input.get("tool_name") != "AskUserQuestion":
        _emit("ask", "tg-approve: not an AskUserQuestion call, deferring")
        return 0

    question, options = _extract_question_and_options(
        hook_input.get("tool_input") or {}
    )
    if not options:
        _emit("ask", "tg-approve: AskUserQuestion had no options to render")
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex[:12]
    text = f"❓ *{question}*\n\n_request id: `{request_id}`_"

    try:
        token = _read_bot_token()
        _post_question(token, text, options, request_id)
    except Exception as e:
        _emit("ask", f"tg-approve send failed: {e}")
        return 0

    chosen = _wait_for_choice(request_id, options)
    if chosen is None:
        _emit("ask", "tg-approve: timeout — no answer in TG, deferring to claude")
        return 0

    # Deny the AskUserQuestion call but stuff the answer in the reason.
    # Claude reads `permissionDecisionReason` and the model sees it as the
    # outcome of the tool call. ccgram does the same trick; until claude
    # ships native AskUserQuestion hook semantics (anthropics/claude-code
    # #12605), this is the cleanest way to short-circuit the tool.
    _emit("deny", f"User selected: {chosen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
