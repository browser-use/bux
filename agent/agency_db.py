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
          source          TEXT,                  -- e.g. slack-c-foo, gmail-thread-19df, gh-pr-78
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
          spawn_topic     INTEGER NOT NULL DEFAULT 0,  -- 1 = Yes-tap creates a fresh topic; 0 = run in-place
          refine_context_injected INTEGER NOT NULL DEFAULT 0,  -- 1 once the worker agent has been seeded with the original card
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
    # Backfill columns on pre-existing tables. ALTER TABLE has no
    # IF NOT EXISTS — swallow the duplicate-column error from re-runs.
    for col, ddl in (
        ("spawn_topic",
         "ALTER TABLE suggestions ADD COLUMN spawn_topic INTEGER NOT NULL DEFAULT 0"),
        ("refine_context_injected",
         "ALTER TABLE suggestions ADD COLUMN refine_context_injected INTEGER NOT NULL DEFAULT 0"),
    ):
        try:
            db.execute(ddl)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
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
    spawn_topic: bool = False,
) -> int:
    cur = db.execute(
        """
        INSERT INTO suggestions (
          title, description, importance, source, prompt, buttons_json,
          tg_chat_id, tg_thread_id, spawn_topic
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            1 if spawn_topic else 0,
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


def is_worker_topic(db: sqlite3.Connection, thread_id: int | None) -> bool:
    """True iff `thread_id` was spawned as the worker_topic for some
    earlier suggestion that lived in a *different* thread.

    Used by `agency-report` to auto-default the spawn-topic flag. When
    the helper is invoked from inside a thread that's already a worker
    for some prior card, the new card defaults to in-place dispatch:
    we're already deep in one task, don't fork another. When the helper
    is invoked from a non-worker thread (the main agency feed, a fresh
    chat, etc.), the new card defaults to spawn=True so each suggestion
    gets its own topic.

    A card whose own `tg_thread_id` equals its `worker_topic_id` is
    excluded — that's the bookkeeping for an in-place dispatch and
    doesn't make the thread a "worker for some other card posted
    elsewhere"."""
    if not thread_id or thread_id <= 0:
        return False
    cur = db.execute(
        """
        SELECT 1 FROM suggestions
         WHERE worker_topic_id = ?
           AND (tg_thread_id IS NULL OR tg_thread_id != worker_topic_id)
         LIMIT 1
        """,
        (int(thread_id),),
    )
    return cur.fetchone() is not None


def find_by_message(
    db: sqlite3.Connection, chat_id: int, message_id: int
) -> dict[str, Any] | None:
    """Return the suggestion row as a plain dict (or None if not found).

    Returning a dict (not the raw sqlite3.Row) lets callers use .get() and
    other dict APIs without surprises — sqlite3.Row supports indexed access
    but not the dict protocol's .get() method.
    """
    cur = db.execute(
        "SELECT * FROM suggestions WHERE tg_chat_id = ? AND tg_message_id = ? LIMIT 1",
        (chat_id, message_id),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


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
    elif any(w in low for w in ("different", "differently", "edit", "refine")):
        status = "differently"
    elif "skip" in low or "no" in low or "don't" in low or "ignore" in low:
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


def find_by_worker_topic(
    db: sqlite3.Connection, thread_id: int | None
) -> dict[str, Any] | None:
    """Return the suggestion whose worker_topic_id == thread_id, if any.
    Filters out in-place rows (worker_topic_id == tg_thread_id)."""
    if not thread_id or thread_id <= 0:
        return None
    cur = db.execute(
        """
        SELECT * FROM suggestions
         WHERE worker_topic_id = ?
           AND (tg_thread_id IS NULL OR tg_thread_id != worker_topic_id)
         ORDER BY id DESC LIMIT 1
        """,
        (int(thread_id),),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def pop_refine_context_for_thread(
    db: sqlite3.Connection, thread_id: int | None
) -> str | None:
    """For Edit (refine) flows: at the user's first reply in the worker
    topic, return the original card's context (title + description +
    prompt) as a plain-text block, AND atomically mark the suggestion as
    `refine_context_injected = 1` so subsequent calls return None.

    Replaces the file-based per-thread context cache the bot used to
    write to /var/lib/bux/agency-refine-context/<thread>.txt. The DB
    already holds the same content; querying it on the user's first
    reply is one SELECT + UPDATE and avoids a separate state surface.

    Returns None when:
      - thread isn't a worker topic for any suggestion
      - the suggestion isn't in 'differently' (Edit-tapped) status
      - context already injected on a prior call
    """
    if not thread_id or thread_id <= 0:
        return None
    cur = db.execute(
        """
        SELECT id, title, description, prompt
          FROM suggestions
         WHERE worker_topic_id = ?
           AND status = 'differently'
           AND refine_context_injected = 0
         ORDER BY id DESC LIMIT 1
        """,
        (int(thread_id),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    parts: list[str] = [f"Original agency card title:\n{row['title'] or ''}"]
    desc = (row["description"] or "").strip()
    if desc:
        parts.append(f"\nOriginal context:\n{desc}")
    prompt = (row["prompt"] or "").strip()
    if prompt:
        parts.append(f"\nOriginal action prompt:\n{prompt}")
    db.execute(
        "UPDATE suggestions SET refine_context_injected = 1, updated_at = ? WHERE id = ?",
        (_now(), int(row["id"])),
    )
    db.commit()
    return "\n".join(parts)
