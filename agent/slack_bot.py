"""Slack bot — Socket Mode listener, claude replies as Harald/Leia.

Listens for messages that mention the configured Slack user (Harald,
U0B38JTEMFH = Leia Bowser) and for DMs sent to that user. Each qualifying
event spawns a `claude -p` turn with the Composio MCP available; claude
decides whether to reply and, if so, posts via SLACK_SEND_MESSAGE as the
connected Slack user (Leia/Harald — no bot badge).

Architecture mirrors bux-tg.service:
  - persistent service (bux-slack.service)
  - Slack pushes events over WebSocket (Socket Mode), zero polling
  - dedupe on message ts in /var/lib/bux/slack-seen.json
  - one claude turn per mention, spawned in a thread

Env (from /etc/bux/slack.env):
  SLACK_APP_TOKEN     — xapp-* app-level token (Socket Mode handshake)
  SLACK_BOT_TOKEN     — xoxb-* bot token (only used for connections.open)
  SLACK_HARALD_USER_ID — Slack user_id whose mentions trigger replies

State:
  /var/lib/bux/slack-seen.json — recent message ts (rolling, last 500)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import httpx
import websockets
from websockets.sync.client import connect as ws_connect

LOG = logging.getLogger("bux-slack")

ENV_FILE = Path("/etc/bux/slack.env")
SEEN_FILE = Path("/var/lib/bux/slack-seen.json")
SEEN_MAX = 500
SLACK_API = "https://slack.com/api"


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _load_seen() -> deque[str]:
    if not SEEN_FILE.exists():
        return deque(maxlen=SEEN_MAX)
    try:
        data = json.loads(SEEN_FILE.read_text())
        return deque(data, maxlen=SEEN_MAX)
    except Exception:
        return deque(maxlen=SEEN_MAX)


def _save_seen(seen: deque[str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(seen)))
    tmp.replace(SEEN_FILE)


def _open_socket_url(app_token: str) -> str:
    """Slack's apps.connections.open returns a one-shot WSS URL with a 30s
    handshake window. We reconnect (call again) whenever the socket closes.
    """
    r = httpx.post(
        f"{SLACK_API}/apps.connections.open",
        headers={"Authorization": f"Bearer {app_token}"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"apps.connections.open failed: {data}")
    return data["url"]


def _is_mention(text: str, user_id: str) -> bool:
    return f"<@{user_id}>" in (text or "")


def _build_prompt(event: dict, user_id: str) -> str:
    """Prompt claude with the raw event + role guidance. Claude has the
    Composio MCP available and can call SLACK_SEND_MESSAGE itself.
    """
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    sender = event.get("user", "")
    text = event.get("text", "")
    is_dm = event.get("channel_type") == "im"

    surface = "DM'd" if is_dm else "mentioned"
    context = (
        f"You are Leia Bowser on Slack (user_id: {user_id}, handle: @harald).\n"
        f"Someone just {surface} you.\n\n"
        f"Channel: {channel}\n"
        f"Thread ts: {thread_ts}\n"
        f"From user_id: {sender}\n"
        f"Message: {text}\n\n"
        "Decide if a reply makes sense. If yes, use the composio MCP "
        "tool SLACK_SEND_MESSAGE to post into the same channel (and the "
        "same thread_ts if it was in a thread). Keep replies short, "
        "warm, and natural — first-person as Leia. Skip the reply "
        "entirely if the message doesn't actually need one (FYI, "
        "broadcast, already answered, etc).\n\n"
        "Do NOT message back asking for clarification — make a judgment "
        "call. If unsure, err on the side of a brief acknowledgement."
    )
    return context


def _dispatch_claude(event: dict, user_id: str) -> None:
    """Run a one-shot `claude -p` turn for this mention. No session
    persistence — each mention is fresh, claude reads its own MCP tools.
    """
    prompt = _build_prompt(event, user_id)
    cmd = [
        "sudo", "-u", "bux", "-H",
        "/usr/bin/claude",
        "--dangerously-skip-permissions",
        "-p",
        prompt,
    ]
    LOG.info("dispatching claude for ts=%s channel=%s", event.get("ts"), event.get("channel"))
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd="/home/bux",
            timeout=300,
        )
        if proc.returncode != 0:
            LOG.warning("claude exit=%d stderr=%s", proc.returncode, proc.stderr[:500])
        else:
            LOG.info("claude done ts=%s stdout_len=%d", event.get("ts"), len(proc.stdout))
    except subprocess.TimeoutExpired:
        LOG.warning("claude timed out ts=%s", event.get("ts"))
    except Exception:
        LOG.exception("claude dispatch failed ts=%s", event.get("ts"))


def _should_react(event: dict, user_id: str) -> bool:
    """Filter rules:
      - must be a message event
      - skip our own messages (we don't reply to ourselves)
      - skip bot messages (avoid bot-loop with our own send via composio)
      - skip message_changed / deleted subtypes (only fresh messages)
      - DMs to us → react
      - channel messages mentioning us → react
    """
    if event.get("type") != "message":
        return False
    # subtypes like message_changed, message_deleted, channel_join, bot_message
    if event.get("subtype"):
        return False
    if event.get("bot_id"):
        return False
    if event.get("user") == user_id:
        return False
    if event.get("channel_type") == "im":
        return True
    return _is_mention(event.get("text", ""), user_id)


def _handle_envelope(env: dict, seen: deque[str], user_id: str) -> None:
    payload = env.get("payload") or {}
    event = payload.get("event") or {}
    ts = event.get("ts")
    if not ts:
        return
    if ts in seen:
        LOG.info("dup ts=%s, skipping", ts)
        return
    if not _should_react(event, user_id):
        return
    seen.append(ts)
    _save_seen(seen)
    threading.Thread(target=_dispatch_claude, args=(event, user_id), daemon=True).start()


def run_loop(env_kv: dict[str, str]) -> None:
    app_token = env_kv["SLACK_APP_TOKEN"]
    user_id = env_kv["SLACK_HARALD_USER_ID"]
    seen = _load_seen()

    backoff = 1
    while True:
        try:
            url = _open_socket_url(app_token)
            LOG.info("socket-mode connecting")
            with ws_connect(url, max_size=2**22) as ws:
                LOG.info("socket-mode connected")
                backoff = 1
                for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    t = msg.get("type")
                    if t == "hello":
                        LOG.info("hello received")
                        continue
                    if t == "disconnect":
                        LOG.info("disconnect requested (%s); reconnecting", msg.get("reason"))
                        break
                    if t in ("events_api", "interactive", "slash_commands"):
                        # ack first, ALWAYS, regardless of whether we handle
                        env_id = msg.get("envelope_id")
                        if env_id:
                            try:
                                ws.send(json.dumps({"envelope_id": env_id}))
                            except Exception:
                                LOG.exception("ack send failed")
                        if t == "events_api":
                            _handle_envelope(msg, seen, user_id)
        except Exception:
            LOG.exception("socket loop crashed; backoff=%ds", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    env_kv = _load_env()
    if not env_kv.get("SLACK_APP_TOKEN"):
        LOG.error("SLACK_APP_TOKEN missing in %s — refusing to start", ENV_FILE)
        return 1
    if not env_kv.get("SLACK_HARALD_USER_ID"):
        LOG.error("SLACK_HARALD_USER_ID missing in %s — refusing to start", ENV_FILE)
        return 1
    LOG.info("bux-slack starting user_id=%s", env_kv["SLACK_HARALD_USER_ID"])
    run_loop(env_kv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
