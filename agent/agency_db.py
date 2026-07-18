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
          blocks_json     TEXT,                  -- JSON list of expandable card blocks
          image_url       TEXT,
          image_file      TEXT,
          source_label    TEXT,
          source_url      TEXT,
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

        CREATE TABLE IF NOT EXISTS agentcard_wallets (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          project_key     TEXT NOT NULL DEFAULT 'default',
          owner_user_id   TEXT NOT NULL DEFAULT '',
          name            TEXT NOT NULL,
          currency        TEXT NOT NULL DEFAULT 'USD',
          status          TEXT CHECK (status IN ('active','disabled')) NOT NULL DEFAULT 'active',
          risk_tier       TEXT NOT NULL DEFAULT 'local_test',
          created_by      TEXT NOT NULL DEFAULT '',
          metadata_json   TEXT,
          created_at      INTEGER NOT NULL,
          updated_at      INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agentcard_wallet_project
          ON agentcard_wallets(project_key, owner_user_id, status);

        CREATE TABLE IF NOT EXISTS agentcard_wallet_ledger (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          wallet_id       INTEGER NOT NULL REFERENCES agentcard_wallets(id),
          entry_type      TEXT CHECK (entry_type IN
                            ('credit','hold','release','capture','refund','adjustment'))
                          NOT NULL,
          effect_cents    INTEGER NOT NULL,
          amount_cents    INTEGER NOT NULL CHECK (amount_cents >= 0),
          source          TEXT NOT NULL DEFAULT '',
          idempotency_key TEXT,
          metadata_json   TEXT,
          created_at      INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agentcard_ledger_wallet
          ON agentcard_wallet_ledger(wallet_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agentcard_ledger_idempotency
          ON agentcard_wallet_ledger(wallet_id, idempotency_key)
          WHERE idempotency_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS agentcard_reservations (
          id                    INTEGER PRIMARY KEY AUTOINCREMENT,
          wallet_id             INTEGER NOT NULL REFERENCES agentcard_wallets(id),
          project_key           TEXT NOT NULL DEFAULT 'default',
          run_id                TEXT NOT NULL DEFAULT '',
          prompt                TEXT NOT NULL DEFAULT '',
          max_amount_cents      INTEGER NOT NULL CHECK (max_amount_cents > 0),
          settled_amount_cents  INTEGER NOT NULL DEFAULT 0 CHECK (settled_amount_cents >= 0),
          released_amount_cents INTEGER NOT NULL DEFAULT 0 CHECK (released_amount_cents >= 0),
          status                TEXT CHECK (status IN
                                  ('reserved','card_requested','settled','cancelled','expired'))
                                NOT NULL DEFAULT 'reserved',
          expires_at            INTEGER,
          idempotency_key       TEXT,
          metadata_json         TEXT,
          created_at            INTEGER NOT NULL,
          updated_at            INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agentcard_reservations_wallet
          ON agentcard_reservations(wallet_id, status, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agentcard_reservation_run
          ON agentcard_reservations(wallet_id, run_id)
          WHERE run_id != '';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agentcard_reservation_idempotency
          ON agentcard_reservations(wallet_id, idempotency_key)
          WHERE idempotency_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS agentcard_cards (
          id                    INTEGER PRIMARY KEY AUTOINCREMENT,
          wallet_id             INTEGER NOT NULL REFERENCES agentcard_wallets(id),
          reservation_id        INTEGER NOT NULL REFERENCES agentcard_reservations(id),
          provider_card_id      TEXT NOT NULL DEFAULT '',
          provider_request_id   TEXT NOT NULL DEFAULT '',
          requested_amount_cents INTEGER NOT NULL CHECK (requested_amount_cents > 0),
          captured_amount_cents INTEGER NOT NULL DEFAULT 0 CHECK (captured_amount_cents >= 0),
          last4                 TEXT NOT NULL DEFAULT '',
          merchant              TEXT NOT NULL DEFAULT '',
          status                TEXT CHECK (status IN
                                  ('provider_not_configured','requested','issued','authorized',
                                   'captured','declined','refunded','cancelled','expired'))
                                NOT NULL DEFAULT 'provider_not_configured',
          evidence_json         TEXT,
          details_returned_at   INTEGER,
          idempotency_key       TEXT,
          created_at            INTEGER NOT NULL,
          updated_at            INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agentcard_cards_wallet
          ON agentcard_cards(wallet_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agentcard_cards_idempotency
          ON agentcard_cards(reservation_id, idempotency_key)
          WHERE idempotency_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS agentcard_card_events (
          id                INTEGER PRIMARY KEY AUTOINCREMENT,
          wallet_card_id    INTEGER NOT NULL REFERENCES agentcard_cards(id),
          event_type        TEXT NOT NULL,
          provider_event_id TEXT NOT NULL DEFAULT '',
          idempotency_key   TEXT,
          payload_json      TEXT,
          created_at        INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agentcard_card_events_card
          ON agentcard_card_events(wallet_card_id, created_at);
        """
    )
    # Backfill columns on pre-existing tables. ALTER TABLE has no
    # IF NOT EXISTS — swallow the duplicate-column error from re-runs.
    for col, ddl in (
        ("spawn_topic",
         "ALTER TABLE suggestions ADD COLUMN spawn_topic INTEGER NOT NULL DEFAULT 0"),
        ("refine_context_injected",
         "ALTER TABLE suggestions ADD COLUMN refine_context_injected INTEGER NOT NULL DEFAULT 0"),
        ("blocks_json",
         "ALTER TABLE suggestions ADD COLUMN blocks_json TEXT"),
        ("image_url",
         "ALTER TABLE suggestions ADD COLUMN image_url TEXT"),
        ("image_file",
         "ALTER TABLE suggestions ADD COLUMN image_file TEXT"),
        ("source_label",
         "ALTER TABLE suggestions ADD COLUMN source_label TEXT"),
        ("source_url",
         "ALTER TABLE suggestions ADD COLUMN source_url TEXT"),
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
    blocks: list[dict] | None = None,
    image_url: str | None = None,
    image_file: str | None = None,
    source_label: str | None = None,
    source_url: str | None = None,
    chat_id: int | None = None,
    thread_id: int | None = None,
    spawn_topic: bool = False,
) -> int:
    cur = db.execute(
        """
        INSERT INTO suggestions (
          title, description, importance, source, prompt, buttons_json, blocks_json, image_url, image_file,
          source_label, source_url,
          tg_chat_id, tg_thread_id, spawn_topic
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            description,
            importance,
            source,
            prompt,
            json.dumps(buttons) if buttons is not None else None,
            json.dumps(blocks) if blocks is not None else None,
            image_url,
            image_file,
            source_label,
            source_url,
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
    elif any(w in low for w in ("more", "regen", "redo", "rethink")):
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


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _require_positive_cents(amount_cents: int, field: str = "amount_cents") -> int:
    try:
        value = int(amount_cents)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer number of cents") from exc
    if value <= 0:
        raise ValueError(f"{field} must be greater than 0")
    return value


def _agentcard_wallet_row(db: sqlite3.Connection, wallet_id: int) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM agentcard_wallets WHERE id = ?",
        (int(wallet_id),),
    ).fetchone()
    if row is None:
        raise ValueError("wallet not found")
    return row


def _agentcard_ledger_effect_cents(db: sqlite3.Connection, wallet_id: int) -> int:
    row = db.execute(
        """
        SELECT COALESCE(SUM(effect_cents), 0) AS balance_cents
          FROM agentcard_wallet_ledger
         WHERE wallet_id = ?
        """,
        (int(wallet_id),),
    ).fetchone()
    return int(row["balance_cents"] or 0) if row else 0


def _agentcard_active_hold_cents(db: sqlite3.Connection, wallet_id: int) -> int:
    row = db.execute(
        """
        SELECT COALESCE(SUM(max_amount_cents - settled_amount_cents - released_amount_cents), 0) AS hold_cents
          FROM agentcard_reservations
         WHERE wallet_id = ?
           AND status IN ('reserved', 'card_requested')
        """,
        (int(wallet_id),),
    ).fetchone()
    return max(0, int(row["hold_cents"] or 0)) if row else 0


def _agentcard_wallet_summary(
    db: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
) -> dict[str, Any]:
    wallet = dict(row)
    wallet_id = int(wallet["id"])
    available_cents = _agentcard_ledger_effect_cents(db, wallet_id)
    active_hold_cents = _agentcard_active_hold_cents(db, wallet_id)
    wallet["available_cents"] = available_cents
    wallet["active_hold_cents"] = active_hold_cents
    wallet["total_balance_cents"] = available_cents + active_hold_cents
    return wallet


def agentcard_create_wallet(
    db: sqlite3.Connection,
    *,
    name: str,
    project_key: str = "default",
    owner_user_id: str = "",
    currency: str = "USD",
    risk_tier: str = "local_test",
    created_by: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("wallet name required")
    currency = str(currency or "USD").upper()
    if len(currency) != 3:
        raise ValueError("currency must be a 3-letter code")
    now = _now()
    cur = db.execute(
        """
        INSERT INTO agentcard_wallets (
          project_key, owner_user_id, name, currency, risk_tier, created_by,
          metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(project_key or "default"),
            str(owner_user_id or ""),
            name[:120],
            currency,
            str(risk_tier or "local_test"),
            str(created_by or ""),
            _json_or_none(metadata),
            now,
            now,
        ),
    )
    db.commit()
    return agentcard_get_wallet(db, int(cur.lastrowid))


def agentcard_get_wallet(db: sqlite3.Connection, wallet_id: int) -> dict[str, Any]:
    return _agentcard_wallet_summary(db, _agentcard_wallet_row(db, wallet_id))


def agentcard_list_wallets(
    db: sqlite3.Connection,
    *,
    project_key: str | None = None,
    owner_user_id: str | None = None,
    include_disabled: bool = False,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if project_key is not None:
        where.append("project_key = ?")
        params.append(str(project_key or "default"))
    if owner_user_id is not None:
        where.append("owner_user_id = ?")
        params.append(str(owner_user_id or ""))
    if not include_disabled:
        where.append("status = 'active'")
    sql = "SELECT * FROM agentcard_wallets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    rows = db.execute(sql, params).fetchall()
    return [_agentcard_wallet_summary(db, row) for row in rows]


def agentcard_fund_wallet(
    db: sqlite3.Connection,
    wallet_id: int,
    *,
    amount_cents: int,
    source: str = "local_admin_grant",
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    amount = _require_positive_cents(amount_cents)
    wallet = _agentcard_wallet_row(db, wallet_id)
    if wallet["status"] != "active":
        raise ValueError("wallet is disabled")
    if idempotency_key:
        existing = db.execute(
            """
            SELECT id FROM agentcard_wallet_ledger
             WHERE wallet_id = ? AND idempotency_key = ?
             LIMIT 1
            """,
            (int(wallet_id), idempotency_key),
        ).fetchone()
        if existing:
            return agentcard_get_wallet(db, wallet_id)
    now = _now()
    db.execute(
        """
        INSERT INTO agentcard_wallet_ledger (
          wallet_id, entry_type, effect_cents, amount_cents, source,
          idempotency_key, metadata_json, created_at
        ) VALUES (?, 'credit', ?, ?, ?, ?, ?, ?)
        """,
        (
            int(wallet_id),
            amount,
            amount,
            str(source or "local_admin_grant"),
            idempotency_key,
            _json_or_none(metadata),
            now,
        ),
    )
    db.execute(
        "UPDATE agentcard_wallets SET updated_at = ? WHERE id = ?",
        (now, int(wallet_id)),
    )
    db.commit()
    return agentcard_get_wallet(db, wallet_id)


def _agentcard_existing_reservation(
    db: sqlite3.Connection,
    wallet_id: int,
    *,
    run_id: str = "",
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    row = None
    if idempotency_key:
        row = db.execute(
            """
            SELECT * FROM agentcard_reservations
             WHERE wallet_id = ? AND idempotency_key = ?
             LIMIT 1
            """,
            (int(wallet_id), idempotency_key),
        ).fetchone()
    if row is None and run_id:
        row = db.execute(
            """
            SELECT * FROM agentcard_reservations
             WHERE wallet_id = ? AND run_id = ?
             LIMIT 1
            """,
            (int(wallet_id), run_id),
        ).fetchone()
    return agentcard_reservation_payload(db, row) if row else None


def agentcard_reserve_wallet(
    db: sqlite3.Connection,
    wallet_id: int,
    *,
    amount_cents: int,
    run_id: str = "",
    prompt: str = "",
    expires_at: int | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    amount = _require_positive_cents(amount_cents, "max_amount_cents")
    wallet = _agentcard_wallet_row(db, wallet_id)
    if wallet["status"] != "active":
        raise ValueError("wallet is disabled")
    existing = _agentcard_existing_reservation(
        db,
        wallet_id,
        run_id=str(run_id or ""),
        idempotency_key=idempotency_key,
    )
    if existing:
        return existing
    available = _agentcard_ledger_effect_cents(db, wallet_id)
    if available < amount:
        raise ValueError("wallet has insufficient available balance")
    now = _now()
    cur = db.execute(
        """
        INSERT INTO agentcard_reservations (
          wallet_id, project_key, run_id, prompt, max_amount_cents, status,
          expires_at, idempotency_key, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?)
        """,
        (
            int(wallet_id),
            str(wallet["project_key"] or "default"),
            str(run_id or ""),
            str(prompt or "")[:2000],
            amount,
            expires_at,
            idempotency_key,
            _json_or_none(metadata),
            now,
            now,
        ),
    )
    reservation_id = int(cur.lastrowid)
    db.execute(
        """
        INSERT INTO agentcard_wallet_ledger (
          wallet_id, entry_type, effect_cents, amount_cents, source,
          idempotency_key, metadata_json, created_at
        ) VALUES (?, 'hold', ?, ?, ?, ?, ?, ?)
        """,
        (
            int(wallet_id),
            -amount,
            amount,
            "reservation",
            f"reservation:{reservation_id}:hold",
            _json_or_none({"reservation_id": reservation_id}),
            now,
        ),
    )
    db.execute(
        "UPDATE agentcard_wallets SET updated_at = ? WHERE id = ?",
        (now, int(wallet_id)),
    )
    db.commit()
    return agentcard_get_reservation(db, reservation_id)


def agentcard_get_reservation(
    db: sqlite3.Connection,
    reservation_id: int,
) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM agentcard_reservations WHERE id = ?",
        (int(reservation_id),),
    ).fetchone()
    if row is None:
        raise ValueError("reservation not found")
    return agentcard_reservation_payload(db, row)


def agentcard_reservation_payload(
    db: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
) -> dict[str, Any]:
    reservation = dict(row)
    remaining = (
        int(reservation["max_amount_cents"])
        - int(reservation["settled_amount_cents"])
        - int(reservation["released_amount_cents"])
    )
    reservation["remaining_hold_cents"] = max(0, remaining)
    reservation["wallet"] = agentcard_get_wallet(db, int(reservation["wallet_id"]))
    return reservation


def agentcard_cancel_reservation(
    db: sqlite3.Connection,
    reservation_id: int,
    *,
    reason: str = "cancelled",
) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM agentcard_reservations WHERE id = ?",
        (int(reservation_id),),
    ).fetchone()
    if row is None:
        raise ValueError("reservation not found")
    if row["status"] in {"cancelled", "settled", "expired"}:
        return agentcard_reservation_payload(db, row)
    remaining = max(
        0,
        int(row["max_amount_cents"])
        - int(row["settled_amount_cents"])
        - int(row["released_amount_cents"]),
    )
    now = _now()
    if remaining:
        db.execute(
            """
            INSERT INTO agentcard_wallet_ledger (
              wallet_id, entry_type, effect_cents, amount_cents, source,
              idempotency_key, metadata_json, created_at
            ) VALUES (?, 'release', ?, ?, 'reservation_cancel', ?, ?, ?)
            """,
            (
                int(row["wallet_id"]),
                remaining,
                remaining,
                f"reservation:{reservation_id}:cancel-release",
                _json_or_none({"reservation_id": int(reservation_id), "reason": reason}),
                now,
            ),
        )
    db.execute(
        """
        UPDATE agentcard_reservations
           SET status = 'cancelled',
               released_amount_cents = released_amount_cents + ?,
               updated_at = ?
         WHERE id = ?
        """,
        (remaining, now, int(reservation_id)),
    )
    db.execute(
        "UPDATE agentcard_wallets SET updated_at = ? WHERE id = ?",
        (now, int(row["wallet_id"])),
    )
    db.commit()
    return agentcard_get_reservation(db, reservation_id)


def agentcard_settle_reservation(
    db: sqlite3.Connection,
    reservation_id: int,
    *,
    captured_amount_cents: int,
    provider_event_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    captured = int(captured_amount_cents)
    if captured < 0:
        raise ValueError("captured_amount_cents must be >= 0")
    row = db.execute(
        "SELECT * FROM agentcard_reservations WHERE id = ?",
        (int(reservation_id),),
    ).fetchone()
    if row is None:
        raise ValueError("reservation not found")
    if row["status"] in {"cancelled", "settled", "expired"}:
        return agentcard_reservation_payload(db, row)
    max_amount = int(row["max_amount_cents"])
    if captured > max_amount:
        raise ValueError("captured amount exceeds reservation")
    release = max_amount - captured
    now = _now()
    db.execute(
        """
        INSERT INTO agentcard_wallet_ledger (
          wallet_id, entry_type, effect_cents, amount_cents, source,
          idempotency_key, metadata_json, created_at
        ) VALUES (?, 'capture', 0, ?, 'provider_capture', ?, ?, ?)
        """,
        (
            int(row["wallet_id"]),
            captured,
            f"reservation:{reservation_id}:capture:{provider_event_id or 'manual'}",
            _json_or_none(metadata),
            now,
        ),
    )
    if release:
        db.execute(
            """
            INSERT INTO agentcard_wallet_ledger (
              wallet_id, entry_type, effect_cents, amount_cents, source,
              idempotency_key, metadata_json, created_at
            ) VALUES (?, 'release', ?, ?, 'reservation_settle', ?, ?, ?)
            """,
            (
                int(row["wallet_id"]),
                release,
                release,
                f"reservation:{reservation_id}:settle-release",
                _json_or_none(metadata),
                now,
            ),
        )
    db.execute(
        """
        UPDATE agentcard_reservations
           SET status = 'settled',
               settled_amount_cents = ?,
               released_amount_cents = ?,
               updated_at = ?
         WHERE id = ?
        """,
        (captured, release, now, int(reservation_id)),
    )
    db.execute(
        """
        UPDATE agentcard_cards
           SET captured_amount_cents = ?,
               status = CASE
                 WHEN status IN ('provider_not_configured', 'requested') THEN status
                 ELSE 'captured'
               END,
               updated_at = ?
         WHERE reservation_id = ?
        """,
        (captured, now, int(reservation_id)),
    )
    db.execute(
        "UPDATE agentcard_wallets SET updated_at = ? WHERE id = ?",
        (now, int(row["wallet_id"])),
    )
    db.commit()
    return agentcard_get_reservation(db, reservation_id)


def agentcard_request_card(
    db: sqlite3.Connection,
    reservation_id: int,
    *,
    requested_amount_cents: int | None = None,
    merchant: str = "",
    evidence: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    reservation = agentcard_get_reservation(db, reservation_id)
    if reservation["status"] in {"cancelled", "settled", "expired"}:
        raise ValueError("reservation is not active")
    remaining = int(reservation["remaining_hold_cents"])
    amount = _require_positive_cents(
        requested_amount_cents if requested_amount_cents is not None else remaining,
        "requested_amount_cents",
    )
    if amount > remaining:
        raise ValueError("requested amount exceeds reservation")
    if idempotency_key:
        existing = db.execute(
            """
            SELECT * FROM agentcard_cards
             WHERE reservation_id = ? AND idempotency_key = ?
             LIMIT 1
            """,
            (int(reservation_id), idempotency_key),
        ).fetchone()
        if existing:
            return agentcard_card_payload(existing)
    now = _now()
    cur = db.execute(
        """
        INSERT INTO agentcard_cards (
          wallet_id, reservation_id, requested_amount_cents, merchant,
          status, evidence_json, idempotency_key, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'provider_not_configured', ?, ?, ?, ?)
        """,
        (
            int(reservation["wallet_id"]),
            int(reservation_id),
            amount,
            str(merchant or "")[:240],
            _json_or_none(evidence),
            idempotency_key,
            now,
            now,
        ),
    )
    card_id = int(cur.lastrowid)
    db.execute(
        """
        INSERT INTO agentcard_card_events (
          wallet_card_id, event_type, idempotency_key, payload_json, created_at
        ) VALUES (?, 'provider_not_configured', ?, ?, ?)
        """,
        (
            card_id,
            f"card:{card_id}:provider_not_configured",
            _json_or_none({"merchant": merchant, "requested_amount_cents": amount}),
            now,
        ),
    )
    db.execute(
        """
        UPDATE agentcard_reservations
           SET status = 'card_requested', updated_at = ?
         WHERE id = ? AND status = 'reserved'
        """,
        (now, int(reservation_id)),
    )
    db.commit()
    row = db.execute("SELECT * FROM agentcard_cards WHERE id = ?", (card_id,)).fetchone()
    return agentcard_card_payload(row)


def agentcard_card_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    card = dict(row)
    card.pop("provider_card_id", None)
    card.pop("provider_request_id", None)
    card["relay_status"] = card["status"]
    card["card_details_available"] = False
    return card


def agentcard_list_cards(
    db: sqlite3.Connection,
    *,
    wallet_id: int | None = None,
    reservation_id: int | None = None,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if wallet_id is not None:
        where.append("wallet_id = ?")
        params.append(int(wallet_id))
    if reservation_id is not None:
        where.append("reservation_id = ?")
        params.append(int(reservation_id))
    sql = "SELECT * FROM agentcard_cards"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    rows = db.execute(sql, params).fetchall()
    return [agentcard_card_payload(row) for row in rows]
