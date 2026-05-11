from __future__ import annotations

import sys
import threading
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

import telegram_bot as tb  # noqa: E402


OWNER_USER_ID = 1234567890


def _bot(monkeypatch):
    bot = tb.Bot.__new__(tb.Bot)
    bot.token = "fake-token"
    bot.api = "https://api.telegram.org/botfake-token"
    bot.state = {"agents": {}, "owners": {}, "offset": 0}
    monkeypatch.setattr(tb.Bot, "send", lambda self, *args, **kwargs: None)
    return bot


def test_minimal_claude_login_includes_copy_button_and_url_fallback(monkeypatch):
    """Claude OAuth links remain copyable even if inline buttons fail."""
    bot = _bot(monkeypatch)
    sends: list[tuple[str, dict]] = []

    def _send_spy(self, chat_id, text, **kwargs):
        sends.append((text, kwargs))

    monkeypatch.setattr(tb.Bot, "send", _send_spy)

    sess = tb.ShellSession.__new__(tb.ShellSession)
    sess.bot = bot
    sess.chat_id = OWNER_USER_ID
    sess.thread_id = 0
    sess.minimal_login_mode = True
    sess._minimal_paste_prompt_sent = False
    sess._buffer = bytearray(
        b"Open https://claude.ai/login?returnTo=/oauth/authorize%3Fclient_id%3Dabc"
    )
    sess._buffer_lock = threading.Lock()
    sess._last_flush = 0.0
    sess._announced_urls = set()
    sess._success_close_started = False
    sess.close_on_success_patterns = ()

    sess._maybe_flush(force=True)

    assert len(sends) == 1
    text, kwargs = sends[0]
    assert "Sign in to Claude" in text
    assert "https://claude.ai/login" in text
    assert "```" in text
    assert kwargs["reply_markup"]["inline_keyboard"][0][0]["copy_text"]["text"].startswith(
        "https://claude.ai/login"
    )


def test_forced_claude_login_restarts_existing_shell(monkeypatch):
    """Auto-login after a 401 should not be blocked by a stale login shell."""
    bot = _bot(monkeypatch)
    calls: list[str] = []

    class Existing:
        def kill(self, reason="cancelled"):
            calls.append(f"kill:{reason}")

    existing = Existing()
    get_calls = {"n": 0}

    def _get_shell_session(slug):
        get_calls["n"] += 1
        return existing if get_calls["n"] == 1 else None

    class FakeShellSession:
        def __init__(self, *args, **kwargs):
            calls.append(f"new:{kwargs.get('initial_cmd')}:{kwargs.get('minimal_login_mode')}")

        def start(self):
            calls.append("start")

    monkeypatch.setattr(tb, "_get_shell_session", _get_shell_session)
    monkeypatch.setattr(tb, "ShellSession", FakeShellSession)
    monkeypatch.setattr(tb, "_login_status_cache_invalidate", lambda name: calls.append(f"invalidate:{name}"))

    bot._cmd_claude_login(
        OWNER_USER_ID,
        reply_to=1,
        thread_id=0,
        slug="lane",
        sender={"user_id": str(OWNER_USER_ID)},
        owner={"user_id": str(OWNER_USER_ID)},
        force_existing=True,
    )

    assert calls == [
        "kill:restart-login",
        "new:claude auth login:True",
        "start",
        "invalidate:claude",
    ]
