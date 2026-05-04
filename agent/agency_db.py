"""Agency DB — persistent SQLite store for every suggestion the Agency loop
posts to Telegram, plus the user's decision (yes/no/different/regenerate/…)
and any worker topic where the resulting work runs.

Why: Magnus wants every Agency suggestion deduped, tracked, and persistent.
If he never responded to a topic, future agency runs should suppress it.
The schema is generalizable — `buttons_json` stores whichever label set
was offered, `decision` records the literal label tapped, so the same
table works for the default 4 buttons and for ad-hoc custom sets like
"Send draft A / Send draft B / Send draft C".

Stored at /var/lib/bux/agency.db (created on first use, owned by `bux`).
This is a small, self-contained module — no migrations framework, no ORM,
no abstraction layer. Just a few helpers.

Public surface:
  conn() -> sqlite3.Connection (init + return)
  init_schema(conn)
  insert(...) -> int                  # suggestion id
  update_message(suggestion_id, message_id)
  record_decision(chat_id, message_id, decision, decision_at)
  set_worker_topic(suggestion_id, worker_topic_id)
  set_status(suggestion_id, status, completed_at=None)
  exists(source) -> dict | None       # latest row for a given source
  search(query, limit=10) -> [row...]  # fuzzy LIKE-search by title/desc
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("BUX_AGENCY_DB", "/var/lib/bux/agency.db"))


def conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    init_schema(db)
    return db


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS suggestions (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          title           TEXT NOT NULL,
          description     TEXT NOT NULL,
          importance      TEXT CHECK (importance IN ('high','med','low')) DEFAULT 'med',
          source          TEXT,                  -- e.g. slack-c-minerva, gmail-thread-19df, gh-pr-78
          prompt          TEXT,                  -- the action that would run if user says yes
          buttons_json    TEXT,                  -- JSON list of the labels shown
          tg_chat_id      INTEGER,
          tg_thread_id    INTEGER,
          tg_message_id   INTEGER,
          status          TEXT CHECK (status IN
                            ('pending','accepted','dismissed','differently',
                             'regenerated','expired','completed','failed'))
                          DEFAULT 'pending',
          decision        TEXT,                  -- the literal label tapped
          decision_at     INTEGER,
          worker_topic_id INTEGER,               -- TG topic where the resulting agent runs
          worker_started_at   INTEGER,
          worker_completed_at INTEGER,
          created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
          updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
        );
        CREATE INDEX IF NOT EXISTS idx_sugg_status      ON suggestions(status);
        CREATE INDEX IF NOT EXISTS idx_sugg_source      ON suggestions(source);
        CREATE INDEX IF NOT EXISTS idx_sugg_created     ON suggestions(created_at);
        CREATE INDEX IF NOT EXISTS idx_sugg_msg         ON suggestions(tg_chat_id, tg_message_id);
        CREATE INDEX IF NOT EXISTS idx_sugg_worker_topic ON suggestions(worker_topic_id);
        """
    )
    db.commit()


def _now() -> int:
    return int(time.time())


def insert(
    db: sqlite3.Connection,
    *,
    title: str,
    description: str,
    importance: str = "med",
    source: str | None = None,
    prompt: str | None = None,
    buttons: list[str] | None = None,
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> int:
    cur = db.execute(
        """
        INSERT INTO suggestions (
          title, description, importance, source, prompt, buttons_json,
          tg_chat_id, tg_thread_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            description,
            importance,
            source,
            prompt,
            json.dumps(buttons) if buttons is not None else None,
            chat_id,
            thread_id,
        ),
    )
    db.commit()
    return int(cur.lastrowid)


def update_message(db: sqlite3.Connection, suggestion_id: int, message_id: int) -> None:
    db.execute(
        "UPDATE suggestions SET tg_message_id = ?, updated_at = ? WHERE id = ?",
        (message_id, _now(), suggestion_id),
    )
    db.commit()


def find_by_message(
    db: sqlite3.Connection, chat_id: int, message_id: int
) -> sqlite3.Row | None:
    cur = db.execute(
        "SELECT * FROM suggestions WHERE tg_chat_id = ? AND tg_message_id = ? LIMIT 1",
        (chat_id, message_id),
    )
    return cur.fetchone()


def record_decision(
    db: sqlite3.Connection,
    chat_id: int,
    message_id: int,
    decision: str,
) -> int | None:
    """Idempotent: locate the row by (chat_id, message_id), set the decision +
    derive a status from the label. Returns the suggestion id, or None if
    no row matched (out-of-band button or message not stored)."""
    row = find_by_message(db, chat_id, message_id)
    if row is None:
        return None
    low = decision.lower()
    if any(w in low for w in ("yes", "do it", "ship", "send", "merge", "approve")):
        status = "accepted"
    elif any(w in low for w in ("regen", "redo", "rethink")):
        status = "regenerated"
    elif any(w in low for w in ("different", "differently")):
        status = "differently"
    elif "no" in low or "skip" in low or "don't" in low or "ignore" in low:
        status = "dismissed"
    else:
        status = "accepted"  # custom labels like "Send draft A" → treat as accept
    db.execute(
        """
        UPDATE suggestions
           SET decision = ?, decision_at = ?, status = ?, updated_at = ?
         WHERE id = ?
        """,
        (decision, _now(), status, _now(), row["id"]),
    )
    db.commit()
    return int(row["id"])


def set_worker_topic(
    db: sqlite3.Connection, suggestion_id: int, worker_topic_id: int
) -> None:
    db.execute(
        """
        UPDATE suggestions
           SET worker_topic_id = ?, worker_started_at = COALESCE(worker_started_at, ?), updated_at = ?
         WHERE id = ?
        """,
        (worker_topic_id, _now(), _now(), suggestion_id),
    )
    db.commit()


def set_status(
    db: sqlite3.Connection,
    suggestion_id: int,
    status: str,
    completed_at: int | None = None,
) -> None:
    db.execute(
        """
        UPDATE suggestions
           SET status = ?, worker_completed_at = COALESCE(?, worker_completed_at), updated_at = ?
         WHERE id = ?
        """,
        (status, completed_at, _now(), suggestion_id),
    )
    db.commit()


def exists(db: sqlite3.Connection, source: str) -> dict[str, Any] | None:
    cur = db.execute(
        "SELECT * FROM suggestions WHERE source = ? ORDER BY id DESC LIMIT 1",
        (source,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def search(
    db: sqlite3.Connection, query: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Fuzzy LIKE-search across title + description. Lower-cases both."""
    q = f"%{query.lower()}%"
    cur = db.execute(
        """
        SELECT * FROM suggestions
         WHERE LOWER(title) LIKE ? OR LOWER(description) LIKE ?
         ORDER BY created_at DESC
         LIMIT ?
        """,
        (q, q, limit),
    )
    return [dict(r) for r in cur.fetchall()]


def list_recent(
    db: sqlite3.Connection, status: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    if status:
        cur = db.execute(
            "SELECT * FROM suggestions WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        )
    else:
        cur = db.execute(
            "SELECT * FROM suggestions ORDER BY id DESC LIMIT ?", (limit,)
        )
    return [dict(r) for r in cur.fetchall()]
