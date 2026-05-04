"""SQLite store for the agency mini app.

Schema is intentionally tiny — three tables:
  suggestions  one swipeable card. status ∈ pending|accepted|dismissed|feedback
  decisions    immutable log of every swipe (right/left/up)
  tasks        right-swipes that were dispatched to the agent
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.environ.get("BUX_AGENCY_DB", "/var/lib/bux/agency.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  draft_action TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggestions_user_status
  ON suggestions(user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  suggestion_id INTEGER NOT NULL,
  user_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('right','left','up')),
  feedback_text TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_user
  ON decisions(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  suggestion_id INTEGER NOT NULL,
  user_id TEXT NOT NULL,
  topic_id INTEGER,
  status TEXT NOT NULL DEFAULT 'queued',
  started_at INTEGER,
  completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_user_status
  ON tasks(user_id, status);
"""


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def now() -> int:
    return int(time.time())


def list_pending(user_id: str, limit: int = 10) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """SELECT id, title, description, draft_action, source, status,
                      created_at, updated_at
               FROM suggestions
               WHERE user_id = ? AND status = 'pending'
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def count_pending(user_id: str) -> int:
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM suggestions WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchone()
    return int(row["n"])


def insert_suggestion(
    user_id: str,
    title: str,
    description: str = "",
    draft_action: str = "",
    source: str = "",
) -> int:
    ts = now()
    with connect() as c:
        cur = c.execute(
            """INSERT INTO suggestions
                 (user_id, title, description, draft_action, source, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (user_id, title, description, draft_action, source, ts, ts),
        )
    return int(cur.lastrowid)


def get_suggestion(suggestion_id: int, user_id: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            """SELECT id, user_id, title, description, draft_action, source, status,
                      created_at, updated_at
               FROM suggestions
               WHERE id = ? AND user_id = ?""",
            (suggestion_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def update_status(suggestion_id: int, user_id: str, status: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE suggestions SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (status, now(), suggestion_id, user_id),
        )


def insert_decision(
    suggestion_id: int, user_id: str, decision: str, feedback_text: str | None
) -> int:
    with connect() as c:
        cur = c.execute(
            """INSERT INTO decisions (suggestion_id, user_id, decision, feedback_text, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (suggestion_id, user_id, decision, feedback_text, now()),
        )
    return int(cur.lastrowid)


def insert_task(suggestion_id: int, user_id: str) -> int:
    with connect() as c:
        cur = c.execute(
            """INSERT INTO tasks (suggestion_id, user_id, status, started_at)
               VALUES (?, ?, 'queued', ?)""",
            (suggestion_id, user_id, now()),
        )
    return int(cur.lastrowid)


def set_task_topic(task_id: int, topic_id: int) -> None:
    with connect() as c:
        c.execute("UPDATE tasks SET topic_id = ? WHERE id = ?", (topic_id, task_id))
