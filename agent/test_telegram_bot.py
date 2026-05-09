"""Tests for the bind-gate decision in telegram_bot.Bot.handle().

The gate has two accept paths: (1) owner-DM auto-bind when cloud propagated
TG_OWNER_ID at install time, and (2) the strict /start <token> match. These
tests cover both paths plus the regression cases (stranger-attack, group-chat
leak, paste-flow path-2-only behavior) without spinning up a real bot or
hitting Telegram.

Mocks file I/O and `Bot.send` so nothing leaves the process. Each test
constructs a synthetic Telegram `message` dict, calls `Bot.handle(msg)`, and
asserts on the resulting calls (`add_allow`, `Bot.send`, etc.).

Run from `bux/` repo root:

    python3 -m pytest agent/test_telegram_bot.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# `agent/` is a flat script dir, not a package — add it to sys.path so the
# import works regardless of where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).parent))

import telegram_bot as tb  # noqa: E402

OWNER_USER_ID = 1234567890
STRANGER_USER_ID = 9999999999
SETUP_TOKEN = "the-real-setup-token"


def _msg_private(sender_user_id: int, text: str | None = "/start") -> dict:
    """A synthetic private-DM message from `sender_user_id`."""
    out: dict = {
        "message_id": 1,
        "chat": {"id": sender_user_id, "type": "private"},
        "from": {"id": sender_user_id, "first_name": "Test", "username": "tester"},
    }
    if text is not None:
        out["text"] = text
    return out


def _msg_group(sender_user_id: int, group_chat_id: int, text: str = "/start") -> dict:
    """A synthetic group-chat message — chat.id != from.id."""
    return {
        "message_id": 2,
        "chat": {"id": group_chat_id, "type": "supergroup"},
        "from": {"id": sender_user_id, "first_name": "Test", "username": "tester"},
        "text": text,
    }


@pytest.fixture
def bot(monkeypatch):
    """Bot instance with all I/O surfaces mocked.

    Constructs the Bot without invoking __init__'s httpx client setup —
    we don't need network and we don't want to hit the Telegram API.
    """
    b = tb.Bot.__new__(tb.Bot)
    b.token = "fake-token"
    b.setup_token = SETUP_TOKEN
    b.api = "https://api.telegram.org/botfake-token"
    b.state = {"agents": {}, "owners": {}, "offset": 0}

    # Make sure load_allow() returns empty unless the test arranges otherwise
    monkeypatch.setattr(tb, "load_allow", lambda: set())
    monkeypatch.setattr(tb, "save_state", lambda s: None)
    monkeypatch.setattr(tb, "add_allow", lambda chat_id: None)
    monkeypatch.setattr(tb, "burn_setup_token", lambda: None)
    # Bot.send hits Telegram — no-op it
    monkeypatch.setattr(tb.Bot, "send", lambda self, *a, **k: None)
    # Track _bind_chat invocations so tests can assert on bind-or-not.
    # `add_allow` is mocked, so we don't mutate any persistent allow-set —
    # the spy just records what `handle()` decided.
    bind_calls: list[tuple[int, dict | None]] = []

    def _bind_chat_spy(self, chat_id, sender=None):
        bind_calls.append((chat_id, sender))

    monkeypatch.setattr(tb.Bot, "_bind_chat", _bind_chat_spy)
    b._bind_calls = bind_calls  # type: ignore[attr-defined]
    return b


def _set_env_owner(monkeypatch, owner_user_id: int | None) -> None:
    """Make _box_owner read this owner from /etc/bux/tg.env."""
    env: dict[str, str] = {}
    if owner_user_id is not None:
        env["TG_OWNER_ID"] = str(owner_user_id)
    monkeypatch.setattr(tb, "_read_kv", lambda path: env)


# ---------------------------------------------------------------------------
# Owner-DM auto-bind path (NEW)
# ---------------------------------------------------------------------------


def test_owner_private_dm_bare_start_binds(bot, monkeypatch):
    """Path 1: TG_OWNER_ID set + owner DMs bare /start → binds."""
    _set_env_owner(monkeypatch, OWNER_USER_ID)
    bot.handle(_msg_private(OWNER_USER_ID, text="/start"))
    assert bot._bind_calls == [(OWNER_USER_ID, {"user_id": str(OWNER_USER_ID), "name": "Test", "username": "tester"})]


def test_owner_private_dm_arbitrary_text_binds(bot, monkeypatch):
    """Path 1: owner's first message can be anything; auto-bind doesn't require /start."""
    _set_env_owner(monkeypatch, OWNER_USER_ID)
    bot.handle(_msg_private(OWNER_USER_ID, text="hello"))
    assert len(bot._bind_calls) == 1
    assert bot._bind_calls[0][0] == OWNER_USER_ID


def test_owner_group_chat_does_not_bind(bot, monkeypatch):
    """Path 1 restriction: group chat must NOT auto-bind even from the owner.

    Falls through to path 2 (strict-token); without a matching token, dropped.
    """
    _set_env_owner(monkeypatch, OWNER_USER_ID)
    bot.handle(_msg_group(OWNER_USER_ID, group_chat_id=-100200300, text="/start"))
    assert bot._bind_calls == []


# ---------------------------------------------------------------------------
# Strict-token path (existing — must still work for paste/QR flows)
# ---------------------------------------------------------------------------


def test_stranger_with_correct_token_binds(bot, monkeypatch):
    """Path 2 (existing): /start <correct_token> from anyone still binds.

    Path 1 fails (from.id != owner). Falls through to path 2 (token match).
    """
    _set_env_owner(monkeypatch, OWNER_USER_ID)
    bot.handle(_msg_private(STRANGER_USER_ID, text=f"/start {SETUP_TOKEN}"))
    assert len(bot._bind_calls) == 1
    assert bot._bind_calls[0][0] == STRANGER_USER_ID


def test_stranger_bare_start_does_not_bind(bot, monkeypatch):
    """Path 1 fails (not owner), path 2 fails (no token) → drop.

    This is the post-PR-90 behavior we MUST preserve. Stranger gets no signal.
    """
    _set_env_owner(monkeypatch, OWNER_USER_ID)
    bot.handle(_msg_private(STRANGER_USER_ID, text="/start"))
    assert bot._bind_calls == []


def test_stranger_with_wrong_token_does_not_bind(bot, monkeypatch):
    _set_env_owner(monkeypatch, OWNER_USER_ID)
    bot.handle(_msg_private(STRANGER_USER_ID, text="/start wrong-token"))
    assert bot._bind_calls == []


# ---------------------------------------------------------------------------
# No owner set (paste / QR flow regression check)
# ---------------------------------------------------------------------------


def test_no_owner_env_falls_through_to_token_path(bot, monkeypatch):
    """When TG_OWNER_ID is absent, behavior is identical to PR #90:
    only the strict /start <token> path binds."""
    _set_env_owner(monkeypatch, None)
    # Bare /start drops
    bot.handle(_msg_private(OWNER_USER_ID, text="/start"))
    assert bot._bind_calls == []
    # /start <correct_token> binds
    bot.handle(_msg_private(OWNER_USER_ID, text=f"/start {SETUP_TOKEN}"))
    assert len(bot._bind_calls) == 1


# ---------------------------------------------------------------------------
# Close-topic button callback (tg-done)
# ---------------------------------------------------------------------------


def _cb(data: str, *, chat_id: int, thread_id: int | None, sender_id: int) -> dict:
    """Synthetic callback_query payload for the close-button tap path."""
    msg: dict = {
        "message_id": 42,
        "chat": {"id": chat_id, "type": "supergroup"},
    }
    if thread_id is not None:
        msg["message_thread_id"] = thread_id
    return {
        "id": "cb-1",
        "data": data,
        "from": {"id": sender_id, "first_name": "Test"},
        "message": msg,
    }


@pytest.fixture
def bot_with_owner(bot, monkeypatch):
    """Bot bound to a chat with OWNER_USER_ID as the recorded owner."""
    chat_id = -100200300
    bot.state["owners"] = {str(chat_id): {"user_id": str(OWNER_USER_ID)}}
    bot._chat_id = chat_id  # type: ignore[attr-defined]
    calls: list[tuple[str, dict]] = []

    def _call_spy(self, method, **params):
        calls.append((method, params))
        # closeForumTopic + answerCallbackQuery + editMessageReplyMarkup
        # all return {ok: True} so the handler proceeds through the happy
        # path. Tests that need the failure branch override this.
        return {"ok": True}

    monkeypatch.setattr(tb.Bot, "call", _call_spy)
    bot._call_log = calls  # type: ignore[attr-defined]
    return bot


def test_close_callback_owner_closes_topic_and_strips_button(bot_with_owner):
    """Owner taps Close topic in a forum topic → bot calls closeForumTopic
    with the topic's thread_id, acks the tap, and strips the button."""
    bot = bot_with_owner
    chat_id = bot._chat_id
    bot._handle_callback_query(_cb(
        "close", chat_id=chat_id, thread_id=7, sender_id=OWNER_USER_ID,
    ))
    methods = [m for m, _ in bot._call_log]
    assert "closeForumTopic" in methods
    close_params = next(p for m, p in bot._call_log if m == "closeForumTopic")
    assert close_params["chat_id"] == chat_id
    assert close_params["message_thread_id"] == 7
    assert any(m == "editMessageReplyMarkup" for m, _ in bot._call_log)
    assert any(m == "answerCallbackQuery" for m, _ in bot._call_log)


def test_close_callback_stranger_blocked(bot_with_owner):
    """Non-owner taps Close topic → alert toast, no closeForumTopic call."""
    bot = bot_with_owner
    bot._handle_callback_query(_cb(
        "close", chat_id=bot._chat_id, thread_id=7, sender_id=STRANGER_USER_ID,
    ))
    methods = [m for m, _ in bot._call_log]
    assert "closeForumTopic" not in methods
    answer = next(p for m, p in bot._call_log if m == "answerCallbackQuery")
    assert answer.get("show_alert") is True
    assert "owner" in (answer.get("text") or "").lower()


def test_close_callback_outside_forum_topic_alerts(bot_with_owner):
    """Owner taps Close topic in the general chat (no message_thread_id)
    → alert toast, no closeForumTopic call (would 400 anyway)."""
    bot = bot_with_owner
    bot._handle_callback_query(_cb(
        "close", chat_id=bot._chat_id, thread_id=None, sender_id=OWNER_USER_ID,
    ))
    methods = [m for m, _ in bot._call_log]
    assert "closeForumTopic" not in methods
    answer = next(p for m, p in bot._call_log if m == "answerCallbackQuery")
    assert answer.get("show_alert") is True
    assert "topic" in (answer.get("text") or "").lower()


def test_close_callback_legacy_thread_form_accepted(bot_with_owner):
    """Legacy callback_data `close:<thread>` still routes to the close
    handler — the encoded thread is ignored in favor of the message's
    own message_thread_id, but the prefix shouldn't be rejected."""
    bot = bot_with_owner
    bot._handle_callback_query(_cb(
        "close:99", chat_id=bot._chat_id, thread_id=7, sender_id=OWNER_USER_ID,
    ))
    methods = [m for m, _ in bot._call_log]
    assert "closeForumTopic" in methods
    close_params = next(p for m, p in bot._call_log if m == "closeForumTopic")
    # message.message_thread_id wins over the callback_data-encoded value.
    assert close_params["message_thread_id"] == 7
