#!/opt/bux/venv/bin/python
"""Per-box Telegram Mini App backend.

Runs on the user's own box and serves a small mobile card feed. Auth is
Telegram Mini App initData validated with this box's TG_BOT_TOKEN; access is
then restricted to the box owner. The frontend is static and every API request
sends initData in X-Telegram-Init-Data.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_AGENT = Path(__file__).resolve().parent
STATIC_DIR = REPO_AGENT / "mini_app_static"
TG_ENV = Path("/etc/bux/tg.env")
TG_STATE = Path("/etc/bux/tg-state.json")
TG_ALLOWED = Path("/etc/bux/tg-allowed.txt")
MINI_DB = Path(os.environ.get("BUX_MINIAPP_DB", "/var/lib/bux/miniapp.db"))
GOALS_FILE = Path(os.environ.get("BUX_GOALS_FILE", "/opt/bux/repo/private/goals.md"))
FEEDBACK_FILE = Path(
    os.environ.get(
        "BUX_AGENCY_FEEDBACK_FILE",
        "/opt/bux/repo/private/feedback_agency_acceptance_signals.md",
    )
)
HOST = os.environ.get("BUX_MINIAPP_HOST", "127.0.0.1")
PORT = int(os.environ.get("BUX_MINIAPP_PORT", "8787"))
AUTH_MAX_AGE_SEC = int(os.environ.get("BUX_MINIAPP_AUTH_MAX_AGE", "86400"))

sys.path.insert(0, str(REPO_AGENT))
import agency_db  # noqa: E402


def _read_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def _tg_env() -> dict[str, str]:
    env = _read_kv(TG_ENV)
    env.update({k: v for k, v in os.environ.items() if k.startswith("TG_")})
    return env


def _box_owner_id() -> str:
    env = _tg_env()
    if env.get("TG_OWNER_ID"):
        return str(env["TG_OWNER_ID"])
    try:
        state = json.loads(TG_STATE.read_text())
    except Exception:
        state = {}
    owner = state.get("box_owner") or {}
    if owner.get("user_id"):
        return str(owner["user_id"])
    owners = state.get("owners") or {}
    for rec in owners.values():
        if isinstance(rec, dict) and rec.get("user_id"):
            return str(rec["user_id"])
    try:
        for raw in TG_ALLOWED.read_text().split():
            chat_id = int(raw.strip())
            if chat_id > 0:
                return str(chat_id)
    except Exception:
        pass
    return ""


def _bot_token() -> str:
    token = _tg_env().get("TG_BOT_TOKEN", "")
    if not token:
        raise PermissionError("TG_BOT_TOKEN missing")
    return token


def _default_chat_id() -> int:
    try:
        for raw in TG_ALLOWED.read_text().split():
            raw = raw.strip()
            if raw:
                return int(raw)
    except Exception:
        return 0
    return 0


def _dev_auth_enabled() -> bool:
    return (
        os.environ.get("BUX_MINIAPP_DEV") == "1"
        and not os.environ.get("BUX_MINIAPP_PUBLIC_URL", "").strip()
    )


def _validate_init_data(init_data: str) -> dict[str, Any]:
    """Validate Telegram Mini App initData and return the decoded user."""
    if _dev_auth_enabled() and init_data == "dev":
        owner_id = _box_owner_id() or "1234567890"
        return {
            "id": int(owner_id),
            "first_name": "Dev",
            "username": "dev",
        }
    parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    values = dict(parsed)
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise PermissionError("missing Telegram initData hash")
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", _bot_token().encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise PermissionError("invalid Telegram initData signature")
    auth_date = int(values.get("auth_date") or "0")
    if auth_date and time.time() - auth_date > AUTH_MAX_AGE_SEC:
        raise PermissionError("Telegram initData expired")
    try:
        user = json.loads(values.get("user") or "{}")
    except json.JSONDecodeError as exc:
        raise PermissionError("invalid Telegram user payload") from exc
    owner_id = _box_owner_id()
    if owner_id and str(user.get("id") or "") != owner_id:
        raise PermissionError("not this box owner")
    return user


def _mini_conn() -> sqlite3.Connection:
    MINI_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(MINI_DB))
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS goals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          context TEXT NOT NULL DEFAULT '',
          cadence TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'active',
          tg_chat_id INTEGER,
          tg_thread_id INTEGER,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS card_comments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          suggestion_id INTEGER NOT NULL,
          body TEXT NOT NULL,
          telegram_user_id TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS card_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          suggestion_id INTEGER NOT NULL,
          event TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          telegram_user_id TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS topics (
          chat_id INTEGER NOT NULL,
          thread_id INTEGER NOT NULL,
          title TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL DEFAULT '',
          updated_at INTEGER NOT NULL,
          PRIMARY KEY (chat_id, thread_id)
        );
        CREATE TABLE IF NOT EXISTS topic_context (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL,
          thread_id INTEGER NOT NULL,
          body TEXT NOT NULL,
          telegram_user_id TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        """
    )
    for ddl in (
        "ALTER TABLE goals ADD COLUMN tg_chat_id INTEGER",
        "ALTER TABLE goals ADD COLUMN tg_thread_id INTEGER",
    ):
        try:
            db.execute(ddl)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    db.commit()
    return db


def _now() -> int:
    return int(time.time())


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _text_response(handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(min(length, 1024 * 1024))
    if not raw:
        return {}
    return json.loads(raw.decode())


def _auth_user(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(handler.path)
    query = urllib.parse.parse_qs(parsed.query)
    init_data = (
        handler.headers.get("X-Telegram-Init-Data", "")
        or query.get("initData", [""])[0]
    )
    return _validate_init_data(init_data)


def _first_goal(db: sqlite3.Connection) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM goals WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _image_data_url(path_value: str | None) -> str:
    if not path_value:
        return ""
    path = Path(path_value).expanduser()
    try:
        resolved = path.resolve()
        if not resolved.exists() or resolved.stat().st_size > 2_000_000:
            return ""
        content_type = "image/png"
        if resolved.suffix.lower() in {".jpg", ".jpeg"}:
            content_type = "image/jpeg"
        elif resolved.suffix.lower() == ".webp":
            content_type = "image/webp"
        elif resolved.suffix.lower() == ".svg":
            content_type = "image/svg+xml"
        data = base64.b64encode(resolved.read_bytes()).decode()
        return f"data:{content_type};base64,{data}"
    except Exception:
        return ""


_VIDEO_PATH_RE = re.compile(r"(/[^\s'\"()<>]+\.(?:mp4|mov|webm))", re.I)


def _media_token(suggestion_id: int, path_value: str) -> str:
    secret = (_bot_token() or "miniapp-dev").encode()
    payload = f"{suggestion_id}:{Path(path_value).resolve()}".encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()[:32]


def _video_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".webm":
        return "video/webm"
    if suffix == ".mov":
        return "video/quicktime"
    return "video/mp4"


def _card_video_path(row: dict[str, Any]) -> str:
    image_file = str(row.get("image_file") or "").strip()
    if image_file and Path(image_file).suffix.lower() in {".mp4", ".mov", ".webm"}:
        return image_file
    haystack = "\n".join(
        str(row.get(key) or "")
        for key in ("prompt", "description", "source_url", "image_url")
    )
    match = _VIDEO_PATH_RE.search(haystack)
    return match.group(1) if match else ""


def _card_visual(row: dict[str, Any]) -> dict[str, str]:
    image_url = (row.get("image_url") or "").strip()
    if image_url.lower().split("?", 1)[0].endswith((".mp4", ".mov", ".webm")):
        return {"kind": "video", "src": image_url}
    video_path = _card_video_path(row)
    if video_path:
        path = Path(video_path).expanduser()
        try:
            resolved = path.resolve()
            if resolved.exists() and resolved.stat().st_size <= 80_000_000:
                suggestion_id = int(row.get("id") or 0)
                query = urllib.parse.urlencode(
                    {"token": _media_token(suggestion_id, str(resolved))}
                )
                return {"kind": "video", "src": f"/api/cards/{suggestion_id}/media?{query}"}
        except Exception:
            pass
    image_data_url = _image_data_url(row.get("image_file"))
    if image_data_url:
        return {"kind": "image", "src": image_data_url}
    if image_url.startswith(("https://", "http://")):
        return {"kind": "image", "src": image_url}
    return {"kind": "none"}


def _button_labels(row: dict[str, Any]) -> list[str]:
    raw = row.get("buttons_json")
    if not raw:
        return []
    try:
        labels = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(labels, list):
        return []
    return [str(label).strip() for label in labels if str(label).strip()]


_TAG_RE = re.compile(r"<[^>]+>")


def _html_block_to_text(value: str) -> str:
    text = value or ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|pre|blockquote)>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _card_blocks(row: dict[str, Any]) -> list[dict[str, str]]:
    raw = row.get("blocks_json")
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(items, list):
        return []
    blocks: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _clean_mobile_text(str(item.get("title") or "Details"))
        emoji = str(item.get("emoji") or "").strip()
        body = _html_block_to_text(str(item.get("body_html") or item.get("body") or ""))
        if not body:
            continue
        blocks.append({"emoji": emoji, "title": title or "Details", "body": body})
    return blocks


def _is_default_action_button(label: str) -> bool:
    normalized = re.sub(r"[^a-z]+", " ", label.lower()).strip()
    return normalized in {"yes", "yes new thread", "do it", "start"}


def _custom_button_prompt(row: dict[str, Any], button_label: str) -> str:
    parts = [f"[agency-button] {button_label}", "\nAgency card context:"]
    if row.get("id") is not None:
        parts.append(f"Suggestion id: {row['id']}")
    title = (row.get("title") or "").strip()
    if title:
        parts.append(f"Title: {title}")
    source = (row.get("source") or "").strip()
    if source:
        parts.append(f"Source: {source}")
    buttons = _button_labels(row)
    if buttons:
        parts.append("Buttons shown: " + " | ".join(buttons))
    description = (row.get("description") or "").strip()
    if description:
        parts.append(f"\nCard context:\n{description}")
    prompt = (row.get("prompt") or "").strip()
    if prompt:
        parts.append(f"\nOriginal action prompt:\n{prompt}")
    parts.append(
        "\nUse the card context to execute the tapped button. If the label names "
        "a path or variant that lives in the lane's local log/draft files, find "
        "the matching entry by source or title before asking the user for more context."
    )
    return "\n".join(parts)


def _clip_text(value: str, limit: int) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


_VISIBLE_ID_RE = re.compile(r"\b[A-Z0-9_:-]{10,}\b|\b\d{5,}\b")
_RAW_COUNTER_RE = re.compile(r"\b(?:N|id|ID|parent|thread)[=#:]?\d+\b")
_FILE_URL_RE = re.compile(r"file://\S+")
_LOCAL_PATH_RE = re.compile(r"(?<!https:)(?<!http:)(?:^|\s)/(?:[\w.-]+/)*[\w.-]+")
_BRACKET_TAG_RE = re.compile(r"\[[^\]]{1,80}\]")
_LONG_SLUG_RE = re.compile(r"\b(?=[A-Za-z0-9_-]{24,}\b)(?=.*[-_])[A-Za-z0-9_-]+\b")
_RICE_SCORE_RE = re.compile(r"\b[CRIE]:\s*\d+(?:\.\d+)?\s*(?:[→>+-]\s*)?", re.I)


def _clean_mobile_text(value: str) -> str:
    text = _BRACKET_TAG_RE.sub("", value or "")
    text = _FILE_URL_RE.sub(" ", text)
    text = _LOCAL_PATH_RE.sub(" ", text)
    text = _RICE_SCORE_RE.sub("", text)
    text = _RAW_COUNTER_RE.sub("", text)
    text = _VISIBLE_ID_RE.sub("", text)
    text = _LONG_SLUG_RE.sub("", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([([{])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]}])", r"\1", text)
    return " ".join(text.split())


def _goal_context() -> dict[str, Any] | None:
    with _mini_conn() as db:
        return _first_goal(db)


def _thread_url(row: dict[str, Any]) -> str:
    chat_id = int(row.get("tg_chat_id") or 0)
    thread_id = int(row.get("tg_thread_id") or 0)
    if chat_id < 0 and thread_id > 0:
        return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{thread_id}"
    return ""


def _thread_title(row: dict[str, Any]) -> str:
    thread_id = int(row.get("tg_thread_id") or 0)
    chat_id = int(row.get("tg_chat_id") or 0)
    if thread_id:
        title = _topic_title(chat_id, thread_id)
        if title:
            return title
    source = _human_topic_title(row.get("source") or "")
    if source:
        return source
    title = _clean_mobile_text(row.get("title") or "")
    if title:
        return _clip_text(title, 28)
    return f"Topic {thread_id}" if thread_id else "General"


def _human_topic_title(source: str) -> str:
    value = re.sub(r"[_-]+", " ", source or "").strip()
    if not value:
        return ""
    parts = [
        part
        for part in value.split()
        if part.lower()
        not in {
            "agency",
            "growth",
            "slack",
            "gmail",
            "thread",
            "topic",
            "miniapp",
            "demo",
            "v2",
            "v3",
        }
        and not re.fullmatch(r"n?\d+|20\d{2}|0x[0-9a-f]+", part.lower())
    ]
    label = " ".join(parts[:4]).strip()
    if not label:
        return ""
    words = {
        "oss": "OSS",
        "gh": "GitHub",
        "github": "GitHub",
        "payg": "PAYG",
        "dm": "DM",
        "dms": "DMs",
        "pr": "PR",
        "cdp": "CDP",
    }
    return " ".join(words.get(part.lower(), part.capitalize()) for part in label.split())[:32]


def _source_url(row: dict[str, Any]) -> str:
    explicit = row.get("source_url") or ""
    if explicit:
        return explicit
    source = row.get("source") or ""
    if source.startswith("gmail-"):
        parts = [part for part in source.split("-") if part]
        if len(parts) >= 2:
            return f"https://mail.google.com/mail/u/0/#inbox/{parts[1]}"
    return _thread_url(row)


STARTER_IDEAS: list[dict[str, Any]] = [
    {
        "source": "miniapp-goal:make-bux-successful",
        "title": "Goal: make bux successful",
        "description": "Build the product into a reliable 24/7 personal agent box that users keep using.",
        "prompt": (
            "Goal-lock card accepted from the Mini App.\n\n"
            "Save this as a high-level Agency goal: make bux successful. "
            "Then inspect concrete connected context and create cards that improve activation, retention, distribution, product quality, or trust. "
            "Do reversible/internal work first and ask only before visible or hard-to-revert actions."
        ),
        "buttons": ["Lock goal"],
        "image_text": "MAKE BUX\nsuccessful",
    },
    {
        "source": "miniapp-goal:get-more-users",
        "title": "Goal: reach 1000 users",
        "description": "Find real distribution openings, draft assets, and ask only before public posts or messages.",
        "prompt": (
            "Goal-lock card accepted from the Mini App.\n\n"
            "Save this as a high-level Agency goal: reach 1000 users. "
            "Scan for specific launch moments, communities, posts, signups, warm intros, and demo angles. "
            "Draft assets before asking; stop at sending, posting, purchasing, or external changes."
        ),
        "buttons": ["Lock goal"],
        "image_text": "1000 USERS\ndraft first",
    },
    {
        "source": "miniapp-goal:make-agency-useful",
        "title": "Goal: make Agency useful every day",
        "description": "Keep the feed concrete, visual when helpful, and tied to what the user already cares about.",
        "prompt": (
            "Goal-lock card accepted from the Mini App.\n\n"
            "Save this as a high-level Agency goal: make Agency mode useful every day. "
            "Read goals and Agency history, avoid repeated skipped themes, and generate only cards tied to a concrete source object."
        ),
        "buttons": ["Lock goal"],
        "image_text": "USEFUL FEED\nconcrete cards",
    },
    {
        "source": "miniapp-goal:stay-healthy-relationships",
        "title": "Goal: stay healthy and keep relationships warm",
        "description": "Suggest low-friction actions for energy, focus, fitness, and important people.",
        "prompt": (
            "Goal-lock card accepted from the Mini App.\n\n"
            "Save this as a high-level Agency goal: stay healthy and keep relationships warm. "
            "Look for practical, specific opportunities and draft messages before asking to send anything."
        ),
        "buttons": ["Lock goal"],
        "image_text": "HEALTH + PEOPLE\nsmall moves",
    },
    {
        "source": "miniapp-goal:plan-incredible-trip",
        "title": "Goal: plan an incredible trip",
        "description": "Research routes, stays, schedules, and tradeoffs; ask before bookings or payments.",
        "prompt": (
            "Goal-lock card accepted from the Mini App.\n\n"
            "Save this as a high-level Agency goal: plan an incredible trip. "
            "Research concrete options, prepare itineraries and booking drafts, and ask before purchases, reservations, or external messages."
        ),
        "buttons": ["Lock goal"],
        "image_text": "TRIP PLAN\noptions ready",
    },
]


STARTER_ACCEPTANCE_SUFFIX = (
    "\n\nBefore creating follow-up cards, lock the user's concrete goal and inspect real connected context. "
    "Do not post generic channel/workflow cards. Every follow-up card must name a specific person, company, thread, repo, PR, incident, signup, page, post, or file. "
    "If you do not have enough concrete context yet, ask one short question about the user's goal or the exact surface to monitor."
)


def _starter_image_url(text: str) -> str:
    return "https://placehold.co/1080x540/111827/ffffff/png?text=" + urllib.parse.quote(text)


def _ensure_starter_cards() -> None:
    if os.environ.get("BUX_MINIAPP_SEED_STARTERS") == "0":
        return
    chat_id = _default_chat_id() or None
    with agency_db.conn() as db:
        for idea in STARTER_IDEAS:
            if agency_db.exists(db, str(idea["source"])):
                continue
            agency_db.insert(
                db,
                title=str(idea["title"]),
                description=str(idea["description"]),
                importance="med",
                source=str(idea["source"]),
                source_label="Starter goal",
                prompt=str(idea["prompt"]) + STARTER_ACCEPTANCE_SUFFIX,
                buttons=list(idea.get("buttons") or ["Start this"]),
                image_url=_starter_image_url(str(idea["image_text"])),
                chat_id=chat_id,
                thread_id=0,
                spawn_topic=False,
            )


def _cards(limit: int = 100) -> list[dict[str, Any]]:
    _ensure_starter_cards()
    with agency_db.conn() as db:
        rows = agency_db.list_recent(db, status="pending", limit=limit)
    with _mini_conn() as mdb:
        comments = {
            int(row["suggestion_id"]): int(row["n"])
            for row in mdb.execute(
                "SELECT suggestion_id, COUNT(*) AS n FROM card_comments GROUP BY suggestion_id"
            )
        }
    cards: list[dict[str, Any]] = []
    for row in rows:
        prompt = _clean_mobile_text((row.get("prompt") or "").strip())
        why = _clean_mobile_text((row.get("description") or "").strip())
        title = _clean_mobile_text(row.get("title") or "Untitled action")
        action = prompt or ""
        cards.append(
            {
                "id": int(row["id"]),
                "title": title,
                "why": why,
                "importance": row.get("importance") or "med",
                "action": action,
                "buttons": _button_labels(row),
                "blocks": _card_blocks(row),
                "source": row.get("source") or "",
                "source_label": row.get("source_label") or "",
                "source_url": _source_url(row),
                "topic_id": int(row.get("tg_thread_id") or 0),
                "topic_title": _thread_title(row),
                "created_at": row.get("created_at"),
                "comments": comments.get(int(row["id"]), 0),
                "visual": _card_visual(row),
            }
        )
    return cards


def _topic_title(chat_id: int, thread_id: int) -> str:
    if not thread_id:
        return ""
    with _mini_conn() as db:
        row = db.execute(
            """
            SELECT title FROM topics
             WHERE thread_id = ? AND (? = 0 OR chat_id = ?)
             ORDER BY updated_at DESC LIMIT 1
            """,
            (thread_id, chat_id, chat_id),
        ).fetchone()
    return str(row["title"] or "") if row else ""


def _upsert_topic(chat_id: int, thread_id: int, title: str, source: str = "miniapp") -> None:
    if not chat_id or not thread_id:
        return
    with _mini_conn() as db:
        db.execute(
            """
            INSERT INTO topics (chat_id, thread_id, title, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, thread_id) DO UPDATE SET
              title = COALESCE(NULLIF(excluded.title, ''), topics.title),
              source = excluded.source,
              updated_at = excluded.updated_at
            """,
            (chat_id, thread_id, title[:128], source, _now()),
        )
        db.commit()


def _topics() -> list[dict[str, Any]]:
    topics: dict[tuple[int, int], dict[str, Any]] = {}
    with _mini_conn() as db:
        for row in db.execute("SELECT * FROM topics ORDER BY updated_at DESC"):
            chat_id = int(row["chat_id"] or 0)
            thread_id = int(row["thread_id"] or 0)
            if thread_id:
                topics[(chat_id, thread_id)] = {
                    "id": f"topic:{thread_id}",
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                "title": (row["title"] or "").strip() or f"Topic {thread_id}",
                    "count": 0,
                }
    for card in _cards(limit=100):
        thread_id = int(card.get("topic_id") or 0)
        if not thread_id:
            continue
        chat_id = 0
        key = next((k for k in topics if k[1] == thread_id), (chat_id, thread_id))
        item = topics.setdefault(
            key,
            {
                "id": f"topic:{thread_id}",
                "chat_id": chat_id,
                "thread_id": thread_id,
                "title": str(card.get("topic_title") or "").strip() or f"Topic {thread_id}",
                "count": 0,
            },
        )
        item["count"] = int(item.get("count") or 0) + (0 if card.get("handled") else 1)
    return sorted(topics.values(), key=lambda item: (-int(item.get("count") or 0), str(item.get("title") or "")))[:30]


def _comments(suggestion_id: int) -> list[dict[str, Any]]:
    with _mini_conn() as db:
        rows = db.execute(
            """
            SELECT body, telegram_user_id, created_at
              FROM card_comments
             WHERE suggestion_id = ?
             ORDER BY id ASC
            """,
            (suggestion_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _stats() -> dict[str, int]:
    with agency_db.conn() as db:
        rows = db.execute(
            "SELECT status, COUNT(*) AS n FROM suggestions GROUP BY status"
        ).fetchall()
    out = {str(row["status"]): int(row["n"]) for row in rows}
    out["open"] = out.get("pending", 0)
    out["done"] = out.get("accepted", 0) + out.get("completed", 0)
    with _mini_conn() as db:
        out["goals"] = int(db.execute("SELECT COUNT(*) AS n FROM goals").fetchone()["n"])
        out["comments"] = int(
            db.execute("SELECT COUNT(*) AS n FROM card_comments").fetchone()["n"]
        )
    return out


def _write_setting(key: str, value: str, user: dict[str, Any]) -> None:
    with _mini_conn() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_by TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            INSERT INTO settings (key, value, updated_by, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_by = excluded.updated_by,
              updated_at = excluded.updated_at
            """,
            (key, value, str(user.get("id") or ""), _now()),
        )
        db.commit()


def _settings() -> dict[str, str]:
    with _mini_conn() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_by TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )
        return {str(row["key"]): str(row["value"]) for row in db.execute("SELECT * FROM settings")}


def _goals_file_text() -> str:
    try:
        text = GOALS_FILE.read_text().strip()
    except FileNotFoundError:
        return ""
    except Exception as exc:
        print(f"bux-miniapp: goals file read failed: {exc}", file=sys.stderr)
        return ""
    return text[:12000]


def _append_goal_file_entry(title: str, context: str, cadence: str = "") -> None:
    now = time.strftime("%Y-%m-%d", time.gmtime())
    lines = [
        "",
        f"## {title}",
        f"- Added: {now}",
    ]
    if cadence:
        lines.append(f"- Cadence: {cadence}")
    if context:
        lines.append(f"- Context: {context.strip()}")
    lines.append("- Preference signals: learn from accepted, skipped, and completed Agency cards before suggesting more.")
    try:
        GOALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not GOALS_FILE.exists() or not GOALS_FILE.read_text().strip():
            GOALS_FILE.write_text(
                "# Goals\n\n"
                "Private high-level goals and Agency preferences for this box.\n"
                "The Agency generator reads this before creating cards and updates it when the user clarifies goals.\n"
            )
        with GOALS_FILE.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        print(f"bux-miniapp: goals file append failed: {exc}", file=sys.stderr)


def _append_dismiss_feedback(row: dict[str, Any], user: dict[str, Any]) -> None:
    title = _clean_mobile_text(row.get("title") or "").strip()
    if not title:
        return
    source_label = _clean_mobile_text(row.get("source_label") or row.get("source") or "").strip()
    source_url = str(row.get("source_url") or "").strip()
    now = time.strftime("%Y-%m-%d", time.gmtime())
    actor = str(user.get("username") or user.get("first_name") or user.get("id") or "miniapp")
    pattern_bits = [title]
    if source_label:
        pattern_bits.append(source_label)
    pattern = " | ".join(pattern_bits)
    lines = [
        "",
        f"- {now}: skipped `{pattern}`",
        f"  - Signal: do not re-pitch this exact card shape unless the underlying source materially changes.",
    ]
    if source_url:
        lines.append(f"  - Source: {source_url}")
    lines.append(f"  - Recorded from: {actor}")
    try:
        FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not FEEDBACK_FILE.exists() or not FEEDBACK_FILE.read_text().strip():
            FEEDBACK_FILE.write_text("# Agency Acceptance Signals\n")
        with FEEDBACK_FILE.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        print(f"bux-miniapp: feedback write failed: {exc}", file=sys.stderr)


def _can_create_telegram_topics() -> bool:
    return os.environ.get("BUX_MINIAPP_DEV") != "1" and MINI_DB == Path("/var/lib/bux/miniapp.db")


def _goal_agent_prompt(
    title: str,
    context: str,
    cadence: str = "",
    *,
    mode: str = "initial",
) -> str:
    count = "10 more" if mode == "more" else "10"
    header = "Generate more Mini App action items." if mode == "more" else "Mini App goal created."
    cadence_line = f"\nCadence or schedule mentioned by the user: {cadence}" if cadence else ""
    goals_text = _goals_file_text()
    goals_block = (
        f"\n\nPrivate goals file ({GOALS_FILE}):\n{goals_text}"
        if goals_text
        else f"\n\nPrivate goals file ({GOALS_FILE}) is empty or missing. First lock the user's high-level goals."
    )
    return (
        f"{header}\n\n"
        f"Goal: {title}\n\n"
        f"User context:\n{context or title}"
        f"{cadence_line}"
        f"{goals_block}\n\n"
        "Use the Agency skill and /opt/bux/repo/agent/AGENCY.md. "
        f"Scan the user's available context and generate {count} high-signal action items for this goal. "
        "This is the generator lane for a personal social feed: create cards the user will want to accept. "
        "Read the goals file and agency.db history first so you do not repeat skipped ideas. "
        "Do all reversible/internal work before posting a card, then ask only at the visible boundary. "
        "If the user explicitly says to work autonomously, that they are going away, or that no approval is needed, switch to Autopilot: do the private/reversible work directly, post concise progress updates in this topic, and create approval cards only for visible/external side effects. "
        "Do not generate generic channel ideas like 'monitor Slack' or 'check GitHub'. "
        "Every concrete card must name a person, company, thread, repo, PR, incident, signup, page, post, or file. "
        "If goals or context are still unknown, create high-level goal-lock cards or ask one short goal question instead of leaving the feed empty. "
        "If the goal is vague, assume the user is a startup founder trying to make the startup successful, "
        "but still ground every card in real context and a specific action. "
        "Post them as Agency cards in this same Telegram topic using the normal agency-report/agency-card flow "
        "so they appear in the Mini App feed for this topic. "
        "Set source_label/source_url to the real platform object; never use https://github.com/browser-use/bux as a generic source for non-GitHub cards. "
        "Keep each card short, concrete, and easy to swipe. Prefer real useful images when available. "
        "If the user mentioned a schedule, set up or propose the recurring monitoring cadence instead of treating it as a one-off."
    )


def _topic_decision_history(thread_id: int, limit: int = 24) -> str:
    if not thread_id:
        return "- No topic-specific decisions yet."
    with agency_db.conn() as db:
        rows = db.execute(
            """
            SELECT title, status, decision, source_label, source_url
              FROM suggestions
             WHERE tg_thread_id = ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (thread_id, limit),
        ).fetchall()
    if not rows:
        return "- No topic-specific decisions yet."
    grouped: dict[str, list[str]] = {
        "accepted/completed": [],
        "pending": [],
        "dismissed": [],
        "other": [],
    }
    for row in rows:
        status = str(row["status"] or "pending")
        title = _clip_text(_clean_mobile_text(row["title"] or ""), 96)
        source = _clip_text(_clean_mobile_text(row["source_label"] or ""), 42)
        decision = _clip_text(_clean_mobile_text(row["decision"] or ""), 38)
        label = title
        if source:
            label += f" [{source}]"
        if decision:
            label += f" -> {decision}"
        if status in {"accepted", "completed"}:
            bucket = "accepted/completed"
        elif status == "pending":
            bucket = "pending"
        elif status == "dismissed":
            bucket = "dismissed"
        else:
            bucket = "other"
        grouped[bucket].append(label)
    lines: list[str] = []
    for bucket, items in grouped.items():
        if items:
            lines.append(f"{bucket}:")
            lines.extend(f"- {item}" for item in items[:8])
    return "\n".join(lines) if lines else "- No topic-specific decisions yet."


def _topic_generate_prompt(thread_id: int, title: str) -> str:
    recent: list[str] = []
    with agency_db.conn() as db:
        rows = db.execute(
            """
            SELECT title FROM suggestions
             WHERE tg_thread_id = ?
             ORDER BY id DESC
             LIMIT 12
            """,
            (thread_id,),
        ).fetchall()
        recent = [str(row["title"] or "").strip() for row in rows if str(row["title"] or "").strip()]
    context = "\n".join(f"- {item}" for item in recent[:8]) or "- No existing cards in this topic yet."
    history = _topic_decision_history(thread_id)
    goals_text = _goals_file_text()
    goals_block = (
        f"\nPrivate goals file ({GOALS_FILE}):\n{goals_text}\n"
        if goals_text
        else f"\nPrivate goals file ({GOALS_FILE}) is empty or missing; ask or suggest high-level goals first.\n"
    )
    return (
        "Generate more Mini App action items.\n\n"
        f"Topic: {title}\n"
        f"Existing recent cards:\n{context}\n"
        f"Recent tap history:\n{history}\n"
        f"{goals_block}\n"
        "Use the Agency skill and /opt/bux/repo/agent/AGENCY.md. "
        "The user explicitly wants more cards/action items for this topic. "
        "Treat this topic as a generator lane. Read the private goals and the existing card history, learn from skipped/accepted decisions, and avoid duplicates. "
        "Do not generate generic channel/workflow ideas. Each card must name a specific person, company, thread, repo, PR, incident, signup, page, post, or file and explain why it moves the topic goal. "
        "Set source_label/source_url to the real platform object; never use the bux GitHub repo URL as a generic source for non-GitHub cards. "
        "If the topic goal is unclear, generate high-level goal-lock cards or ask one short clarifying goal question instead of posting filler. "
        "Generate 10 more high-signal cards in this same Telegram topic through the normal agency-report/agency-card flow "
        "so they appear in the Mini App feed for this topic."
    )


def _autopilot_prompt(title: str, context: str = "") -> str:
    topic_match = re.search(r"\bTopic id:\s*(\d+)", context or "", re.I)
    history = _topic_decision_history(int(topic_match.group(1))) if topic_match else ""
    history_block = f"Recent tap history:\n{history}\n" if history else ""
    goals_text = _goals_file_text()
    goals_block = (
        f"\nPrivate goals file ({GOALS_FILE}):\n{goals_text}\n"
        if goals_text
        else f"\nPrivate goals file ({GOALS_FILE}) is empty or missing; infer a practical first goal and ask only if blocked.\n"
    )
    return (
        "Mini App Autopilot started.\n\n"
        f"Goal: {title or 'Agency'}\n"
        f"Context:\n{context or title or 'Improve this goal autonomously.'}\n"
        f"{history_block}"
        f"{goals_block}\n"
        "Work autonomously now. Do not just create approval cards unless you genuinely need a visible human decision. "
        "Do all reversible/internal work directly: inspect files, improve the app, draft assets, analyze data, create local artifacts, and test. "
        "Ask for approval only before actions that send messages, post publicly, buy/pay, delete hard-to-recover data, or affect other people. "
        "Post concise progress updates back into this Telegram topic so the user can see what is happening. "
        "For the Agency app specifically, optimize for a simple goal-driven product: persistent goals, clear permission boundaries, concrete useful cards, and fast one-card decisions. "
        "When finished, report what changed, what was tested, and what remains."
    )


def _ensure_goal_topic(goal_id: int, title: str) -> tuple[int, int]:
    with _mini_conn() as db:
        row = db.execute(
            "SELECT tg_chat_id, tg_thread_id FROM goals WHERE id = ?",
            (goal_id,),
        ).fetchone()
        if row and int(row["tg_chat_id"] or 0) and int(row["tg_thread_id"] or 0):
            return int(row["tg_chat_id"]), int(row["tg_thread_id"])
    chat_id = _default_chat_id()
    if not chat_id or not _can_create_telegram_topics():
        return 0, 0
    try:
        import telegram_bot

        env = _tg_env()
        bot = telegram_bot.Bot(env["TG_BOT_TOKEN"], env.get("TG_SETUP_TOKEN", ""))
        res = bot.call("createForumTopic", chat_id=chat_id, name=title[:128])
        if not res.get("ok"):
            return 0, 0
        thread_id = int(res["result"].get("message_thread_id") or 0)
        if not thread_id:
            return 0, 0
        _upsert_topic(chat_id, thread_id, title, "miniapp-goal")
        with _mini_conn() as db:
            db.execute(
                "UPDATE goals SET tg_chat_id = ?, tg_thread_id = ?, updated_at = ? WHERE id = ?",
                (chat_id, thread_id, _now(), goal_id),
            )
            db.commit()
        return chat_id, thread_id
    except Exception as exc:
        print(f"bux-miniapp: goal topic create failed: {exc}", file=sys.stderr)
        return 0, 0


def _append_event(suggestion_id: int, event: str, user: dict[str, Any], detail: str = "") -> None:
    with _mini_conn() as db:
        db.execute(
            """
            INSERT INTO card_events (suggestion_id, event, detail, telegram_user_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (suggestion_id, event, detail, str(user.get("id") or ""), _now()),
        )
        db.commit()


def _dispatch_topic_context(
    chat_id: int,
    thread_id: int,
    comment: str,
    user: dict[str, Any],
    reply_to: int | None = None,
    heading: str = "Context from Mini App",
) -> bool:
    if not chat_id or not comment:
        return False
    try:
        import telegram_bot

        env = _tg_env()
        bot = telegram_bot.Bot(env["TG_BOT_TOKEN"], env.get("TG_SETUP_TOKEN", ""))
        provider = (_settings().get("provider") or "").strip().lower()
        if provider in {"codex", "claude"} and hasattr(telegram_bot, "_set_agent_for"):
            telegram_bot._set_agent_for((chat_id, thread_id), provider, bot.state)
        text = f"{heading}:\n" + comment
        sent = bot.call(
            "sendMessage",
            chat_id=chat_id,
            message_thread_id=thread_id or None,
            text=text,
            reply_parameters={"message_id": reply_to} if reply_to else None,
        )
        reply_to_agent = int((sent.get("result") or {}).get("message_id") or 0) or None

        def run() -> None:
            try:
                bot.run_task(
                    (chat_id, thread_id),
                    text,
                    reply_to=reply_to_agent,
                    sender={
                        "user_id": str(user.get("id") or ""),
                        "username": user.get("username") or "",
                        "name": user.get("first_name") or "",
                    },
                )
            except Exception:
                print("bux-miniapp: context dispatch failed", file=sys.stderr)

        threading.Thread(target=run, name=f"miniapp-context-{thread_id}", daemon=True).start()
        return True
    except Exception as exc:
        print(f"bux-miniapp: context dispatch failed: {exc}", file=sys.stderr)
        return False


def _dispatch_card_context(row: dict[str, Any], comment: str, user: dict[str, Any]) -> bool:
    chat_id = int(row.get("tg_chat_id") or 0) or _default_chat_id()
    thread_id = int(row.get("worker_topic_id") or row.get("tg_thread_id") or 0)
    if not thread_id:
        return False
    source_thread_id = int(row.get("tg_thread_id") or 0)
    reply_to = int(row.get("tg_message_id") or 0) if thread_id == source_thread_id else None
    return _dispatch_topic_context(chat_id, thread_id, comment, user, reply_to=reply_to)


def _find_suggestion(suggestion_id: int) -> dict[str, Any] | None:
    with agency_db.conn() as db:
        row = db.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        return dict(row) if row else None


def _start_agent_prompt(row: dict[str, Any], action_prompt: str, button_label: str) -> str:
    blocks = _card_blocks(row)
    block_text = ""
    if blocks:
        rendered = []
        for block in blocks:
            title = str(block.get("title") or "Details").strip()
            body = str(block.get("body") or "").strip()
            if body:
                rendered.append(f"- {title}:\n{body}")
        if rendered:
            block_text = "\n\nExpandable card sections:\n" + "\n\n".join(rendered)
    source_bits = []
    if row.get("source_label"):
        source_bits.append(str(row.get("source_label")))
    if row.get("source_url"):
        source_bits.append(str(row.get("source_url")))
    source_line = " ".join(source_bits).strip() or str(row.get("source") or "").strip() or "unknown"
    picked = button_label or "Mini App Start"
    return (
        "The user accepted this Mini App card. Work from the full card context below.\n"
        "This accepted card is now its own worker session. Complete the task in this session when possible. "
        "If you need more user confirmation, post a follow-up Agency card linked to this task instead of asking vaguely. "
        "Do all private/reversible work first and stop only before a visible third-party action.\n\n"
        f"Picked button: {picked}\n"
        f"Card title: {row.get('title') or 'Action'}\n"
        f"Why it matters: {row.get('description') or ''}\n"
        f"Source: {source_line}"
        f"{block_text}\n\n"
        f"Action prompt:\n{action_prompt}"
    )


def _start_agent_work(
    suggestion_id: int, user: dict[str, Any], button_label: str | None = None
) -> dict[str, Any]:
    row = _find_suggestion(suggestion_id)
    if not row:
        return {"started": False, "error": "card not found"}
    prompt = (row.get("prompt") or "").strip()
    button_label = (button_label or "").strip()
    if button_label and not _is_default_action_button(button_label):
        prompt = _custom_button_prompt(row, button_label)
    if not prompt:
        return {"started": False, "error": "card has no action prompt"}
    dispatch_prompt = _start_agent_prompt(row, prompt, button_label)
    try:
        import telegram_bot

        env = _tg_env()
        bot = telegram_bot.Bot(env["TG_BOT_TOKEN"], env.get("TG_SETUP_TOKEN", ""))
        chat_id = int(row.get("tg_chat_id") or 0) or _default_chat_id()
        if not chat_id:
            return {"started": False, "error": "no Telegram chat bound"}
        thread_id = int(row.get("tg_thread_id") or 0)
        work_thread = int(row.get("worker_topic_id") or 0) or thread_id
        topic_created = False
        topic_name = (row.get("title") or "Mini App task")[:128]
        if not int(row.get("worker_topic_id") or 0):
            res = bot.call("createForumTopic", chat_id=chat_id, name=topic_name)
            if res.get("ok"):
                work_thread = int(res["result"].get("message_thread_id") or thread_id)
                topic_created = True
                _upsert_topic(chat_id, work_thread, topic_name, "miniapp-start")
        provider = (_settings().get("provider") or "").strip().lower()
        if provider in {"codex", "claude"} and hasattr(telegram_bot, "_set_agent_for"):
            telegram_bot._set_agent_for((chat_id, work_thread), provider, bot.state)
        with agency_db.conn() as db:
            if row.get("tg_chat_id") and row.get("tg_message_id"):
                agency_db.record_decision(
                    db,
                    int(row.get("tg_chat_id") or 0),
                    int(row.get("tg_message_id") or 0),
                button_label or "Mini App Start",
                )
            agency_db.set_status(db, suggestion_id, "accepted")
            if work_thread:
                agency_db.set_worker_topic(db, suggestion_id, work_thread)
        bot.call(
            "sendMessage",
            chat_id=chat_id,
            message_thread_id=work_thread or None,
            text=(
                "📋 <b>Started from Mini App</b>\n"
                f"<b>{html.escape(row.get('title') or 'Action', quote=False)}</b>\n"
                f"<blockquote>{html.escape(dispatch_prompt, quote=False)}</blockquote>"
            ),
            parse_mode="HTML",
        )

        def run() -> None:
            try:
                bot.run_task(
                    (chat_id, work_thread),
                    dispatch_prompt,
                    reply_to=None,
                    sender={
                        "user_id": str(user.get("id") or ""),
                        "username": user.get("username") or "",
                        "name": user.get("first_name") or "",
                    },
                )
            except Exception:
                print("bux-miniapp: agent dispatch failed", file=sys.stderr)

        threading.Thread(
            target=run, name=f"miniapp-card-{suggestion_id}", daemon=True
        ).start()
        return {
            "started": True,
            "chat_id": chat_id,
            "thread_id": work_thread,
            "topic_created": topic_created,
        }
    except Exception as exc:
        print(f"bux-miniapp: start failed: {exc}", file=sys.stderr)
        return {"started": False, "error": str(exc)}


class MiniAppHandler(BaseHTTPRequestHandler):
    server_version = "bux-miniapp/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("bux-miniapp " + fmt % args + "\n")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        parts = path.strip("/").split("/")
        if path == "/health":
            _json_response(self, 200, {"ok": True})
            return
        if path == "/api/me":
            try:
                user = _auth_user(self)
                _json_response(
                    self,
                    200,
                    {
                        "user": user,
                        "owner_id": _box_owner_id(),
                        "settings": _settings(),
                    },
                )
            except PermissionError as exc:
                _json_response(self, 401, {"error": str(exc)})
            return
        if path == "/api/goals":
            try:
                _auth_user(self)
                with _mini_conn() as db:
                    goals = [dict(row) for row in db.execute("SELECT * FROM goals ORDER BY id DESC")]
                _json_response(self, 200, {"goals": goals})
            except PermissionError as exc:
                _json_response(self, 401, {"error": str(exc)})
            return
        if path == "/api/topics":
            try:
                _auth_user(self)
                _json_response(self, 200, {"topics": _topics()})
            except PermissionError as exc:
                _json_response(self, 401, {"error": str(exc)})
            return
        if path == "/api/settings":
            try:
                _auth_user(self)
                _json_response(self, 200, {"settings": _settings()})
            except PermissionError as exc:
                _json_response(self, 401, {"error": str(exc)})
            return
        if path == "/api/cards":
            try:
                _auth_user(self)
                _json_response(self, 200, {"cards": _cards()})
            except PermissionError as exc:
                _json_response(self, 401, {"error": str(exc)})
            return
        if len(parts) == 4 and parts[:2] == ["api", "cards"] and parts[3] == "comments":
            try:
                _auth_user(self)
                suggestion_id = int(parts[2])
                if not _find_suggestion(suggestion_id):
                    _json_response(self, 404, {"error": "card not found"})
                    return
                _json_response(self, 200, {"comments": _comments(suggestion_id)})
            except PermissionError as exc:
                _json_response(self, 401, {"error": str(exc)})
            return
        if len(parts) == 4 and parts[:2] == ["api", "cards"] and parts[3] == "media":
            suggestion_id = int(parts[2])
            row = _find_suggestion(suggestion_id)
            if not row:
                _json_response(self, 404, {"error": "card not found"})
                return
            video_path = _card_video_path(row)
            if not video_path:
                _json_response(self, 404, {"error": "media not found"})
                return
            path_obj = Path(video_path).expanduser()
            try:
                resolved = path_obj.resolve()
                token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
                if token != _media_token(suggestion_id, str(resolved)):
                    _json_response(self, 403, {"error": "bad media token"})
                    return
                if not resolved.exists() or resolved.stat().st_size > 80_000_000:
                    _json_response(self, 404, {"error": "media not found"})
                    return
                size = resolved.stat().st_size
                start, end = 0, size - 1
                status = 200
                range_header = self.headers.get("Range", "")
                if range_header.startswith("bytes="):
                    status = 206
                    raw_start, _, raw_end = range_header.removeprefix("bytes=").partition("-")
                    start = int(raw_start or "0")
                    end = int(raw_end) if raw_end else end
                    end = min(end, size - 1)
                length = max(0, end - start + 1)
                self.send_response(status)
                self.send_header("Content-Type", _video_content_type(resolved))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                with resolved.open("rb") as fh:
                    fh.seek(start)
                    self.wfile.write(fh.read(length))
            except Exception:
                _json_response(self, 404, {"error": "media not found"})
            return
        if path == "/api/stats":
            try:
                _auth_user(self)
                _json_response(self, 200, {"stats": _stats()})
            except PermissionError as exc:
                _json_response(self, 401, {"error": str(exc)})
            return
        if path == "/":
            path = "/tinder.html"
        if path == "/feed":
            path = "/index.html"
        if path == "/tinder":
            path = "/tinder.html"
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            return
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists():
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        content_type = "text/plain"
        if target.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        _text_response(self, 200, target.read_bytes(), content_type)

    def do_POST(self) -> None:
        try:
            user = _auth_user(self)
            body = _read_json(self)
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.strip("/").split("/")
            if parsed.path == "/api/goals":
                title = (body.get("title") or "").strip()
                context = (body.get("context") or "").strip()
                cadence = (body.get("cadence") or "").strip()
                if not title:
                    _json_response(self, 400, {"error": "title required"})
                    return
                now = _now()
                active_id = ""
                dispatched = False
                with _mini_conn() as db:
                    cur = db.execute(
                        """
                        INSERT INTO goals (title, context, cadence, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (title, context, cadence, now, now),
                    )
                    db.commit()
                    goal_id = int(cur.lastrowid)
                _append_goal_file_entry(title, context, cadence)
                chat_id, thread_id = _ensure_goal_topic(goal_id, title)
                if chat_id and thread_id:
                    active_id = f"topic:{thread_id}"
                    dispatched = _dispatch_topic_context(
                        chat_id,
                        thread_id,
                        _goal_agent_prompt(title, context, cadence),
                        user,
                        heading="Mini App goal created",
                    )
                _json_response(
                    self,
                    200,
                    {"ok": True, "goal_id": goal_id, "active_id": active_id, "dispatched": dispatched},
                )
                return
            if parsed.path == "/api/settings":
                provider = (body.get("provider") or "").strip().lower()
                if provider and provider not in {"codex", "claude"}:
                    _json_response(self, 400, {"error": "provider must be codex or claude"})
                    return
                if provider:
                    _write_setting("provider", provider, user)
                _json_response(self, 200, {"ok": True, "settings": _settings()})
                return
            if len(path) == 4 and path[:2] == ["api", "goals"] and path[3] == "context":
                goal_id = int(path[2])
                comment = (body.get("comment") or "").strip()
                if not comment:
                    _json_response(self, 400, {"error": "comment required"})
                    return
                with _mini_conn() as db:
                    row = db.execute(
                        "SELECT title, context, cadence FROM goals WHERE id = ?",
                        (goal_id,),
                    ).fetchone()
                    if not row:
                        _json_response(self, 404, {"error": "goal not found"})
                        return
                    existing = str(row["context"] or "").strip()
                    updated = (existing + "\n\n" if existing else "") + comment
                    db.execute(
                        "UPDATE goals SET context = ?, updated_at = ? WHERE id = ?",
                        (updated, _now(), goal_id),
                    )
                    db.commit()
                title = str(row["title"] or "Mini App goal")
                cadence = str(row["cadence"] or "")
                chat_id, thread_id = _ensure_goal_topic(goal_id, title)
                dispatched = False
                if chat_id and thread_id:
                    dispatched = _dispatch_topic_context(
                        chat_id,
                        thread_id,
                        _goal_agent_prompt(title, updated, cadence),
                        user,
                        heading="Mini App goal context added",
                    )
                _json_response(self, 200, {"ok": True, "dispatched": dispatched})
                return
            if len(path) == 4 and path[:2] == ["api", "goals"] and path[3] == "generate":
                goal_id = int(path[2])
                with _mini_conn() as db:
                    row = db.execute(
                        "SELECT title, context, cadence FROM goals WHERE id = ?",
                        (goal_id,),
                    ).fetchone()
                if not row:
                    _json_response(self, 404, {"error": "goal not found"})
                    return
                title = str(row["title"] or "Mini App goal")
                chat_id, thread_id = _ensure_goal_topic(goal_id, title)
                dispatched = False
                if chat_id and thread_id:
                    dispatched = _dispatch_topic_context(
                        chat_id,
                        thread_id,
                        _goal_agent_prompt(
                            title,
                            str(row["context"] or ""),
                            str(row["cadence"] or ""),
                            mode="more",
                        ),
                        user,
                        heading="Mini App generate more",
                    )
                _json_response(self, 200, {"ok": True, "dispatched": dispatched})
                return
            if len(path) == 4 and path[:2] == ["api", "goals"] and path[3] == "autopilot":
                goal_id = int(path[2])
                with _mini_conn() as db:
                    row = db.execute(
                        "SELECT title, context, cadence FROM goals WHERE id = ?",
                        (goal_id,),
                    ).fetchone()
                if not row:
                    _json_response(self, 404, {"error": "goal not found"})
                    return
                title = str(row["title"] or "Mini App goal")
                chat_id, thread_id = _ensure_goal_topic(goal_id, title)
                dispatched = False
                if chat_id and thread_id:
                    dispatched = _dispatch_topic_context(
                        chat_id,
                        thread_id,
                        _autopilot_prompt(title, str(row["context"] or "")),
                        user,
                        heading="Mini App Autopilot",
                    )
                _json_response(self, 200, {"ok": True, "dispatched": dispatched})
                return
            if len(path) == 4 and path[:2] == ["api", "topics"] and path[3] == "generate":
                thread_id = int(path[2])
                chat_id = _default_chat_id()
                topic = next((item for item in _topics() if int(item.get("thread_id") or 0) == thread_id), None)
                title = str((topic or {}).get("title") or _topic_title(chat_id, thread_id) or f"Topic {thread_id}")
                dispatched = _dispatch_topic_context(
                    chat_id,
                    thread_id,
                    _topic_generate_prompt(thread_id, title),
                    user,
                    heading="Mini App generate more",
                )
                _json_response(self, 200, {"ok": True, "dispatched": dispatched})
                return
            if len(path) == 4 and path[:2] == ["api", "topics"] and path[3] == "autopilot":
                thread_id = int(path[2])
                chat_id = _default_chat_id()
                topic = next((item for item in _topics() if int(item.get("thread_id") or 0) == thread_id), None)
                title = str((topic or {}).get("title") or _topic_title(chat_id, thread_id) or f"Topic {thread_id}")
                dispatched = _dispatch_topic_context(
                    chat_id,
                    thread_id,
                    _autopilot_prompt(title, f"Continue this goal/topic autonomously. Topic id: {thread_id}."),
                    user,
                    heading="Mini App Autopilot",
                )
                _json_response(self, 200, {"ok": True, "dispatched": dispatched})
                return
            if parsed.path == "/api/generate":
                chat_id = _default_chat_id()
                dispatched = _dispatch_topic_context(
                    chat_id,
                    0,
                    _goal_agent_prompt("General Agency feed", "Generate fresh action items for the user.", mode="more"),
                    user,
                    heading="Mini App generate more",
                )
                _json_response(self, 200, {"ok": True, "dispatched": dispatched})
                return
            if parsed.path == "/api/autopilot":
                chat_id = _default_chat_id()
                dispatched = _dispatch_topic_context(
                    chat_id,
                    0,
                    _autopilot_prompt("General Agency feed", "Improve Agency autonomously from the user's goals and recent card history."),
                    user,
                    heading="Mini App Autopilot",
                )
                _json_response(self, 200, {"ok": True, "dispatched": dispatched})
                return
            if len(path) >= 4 and path[:2] == ["api", "cards"]:
                suggestion_id = int(path[2])
                action = path[3]
                row = _find_suggestion(suggestion_id)
                if not row:
                    _json_response(self, 404, {"error": "card not found"})
                    return
                if action == "comment":
                    comment = (body.get("comment") or "").strip()
                    if not comment:
                        _json_response(self, 400, {"error": "comment required"})
                        return
                    with _mini_conn() as db:
                        db.execute(
                            """
                            INSERT INTO card_comments
                              (suggestion_id, body, telegram_user_id, created_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (suggestion_id, comment, str(user.get("id") or ""), _now()),
                        )
                        db.commit()
                    _append_event(suggestion_id, "comment", user, comment)
                    dispatched = _dispatch_card_context(row, comment, user)
                    _json_response(self, 200, {"ok": True, "dispatched": dispatched})
                    return
                if action == "dismiss":
                    with agency_db.conn() as db:
                        agency_db.set_status(db, suggestion_id, "dismissed")
                    _append_event(suggestion_id, "dismiss", user)
                    _append_dismiss_feedback(row, user)
                    _json_response(self, 200, {"ok": True})
                    return
                if action == "different":
                    detail = (body.get("comment") or "").strip()
                    with agency_db.conn() as db:
                        agency_db.set_status(db, suggestion_id, "differently")
                    _append_event(suggestion_id, "different", user, detail)
                    _json_response(self, 200, {"ok": True})
                    return
                if action == "start":
                    button_label = (body.get("button") or "").strip()
                    _append_event(suggestion_id, "start", user, button_label)
                    result = _start_agent_work(suggestion_id, user, button_label)
                    status = 200 if result.get("started") else 409
                    _json_response(self, status, {"ok": bool(result.get("started")), **result})
                    return
            if len(path) == 4 and path[:2] == ["api", "topics"] and path[3] == "context":
                thread_id = int(path[2])
                comment = (body.get("comment") or "").strip()
                if not comment:
                    _json_response(self, 400, {"error": "comment required"})
                    return
                topic = next((item for item in _topics() if int(item["thread_id"]) == thread_id), None)
                chat_id = int((topic or {}).get("chat_id") or 0) or _default_chat_id()
                with _mini_conn() as db:
                    db.execute(
                        """
                        INSERT INTO topic_context
                          (chat_id, thread_id, body, telegram_user_id, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (chat_id, thread_id, comment, str(user.get("id") or ""), _now()),
                    )
                    db.commit()
                dispatched = _dispatch_topic_context(chat_id, thread_id, comment, user)
                _json_response(self, 200, {"ok": True, "dispatched": dispatched})
                return
            _json_response(self, 404, {"error": "not found"})
        except PermissionError as exc:
            _json_response(self, 401, {"error": str(exc)})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), MiniAppHandler)
    print(f"bux-miniapp listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
