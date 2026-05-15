from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_DIR))

import telegram_bot  # noqa: E402


class CodexSettingsTest(unittest.TestCase):
    def test_codex_settings_are_per_lane(self) -> None:
        state = {"offset": 0, "agents": {}, "codex_settings": {}, "owners": {}}
        first = (1, 10)
        second = (1, 20)

        with mock.patch.object(telegram_bot, "save_state"):
            telegram_bot._set_codex_settings(
                first,
                state,
                model="gpt-5.4-mini",
                reasoning_effort="low",
            )

        self.assertEqual(
            telegram_bot._codex_settings_for(first, state),
            {"model": "gpt-5.4-mini", "reasoning_effort": "low"},
        )
        self.assertEqual(telegram_bot._codex_settings_for(second, state), {})

    def test_clear_codex_settings(self) -> None:
        state = {
            "offset": 0,
            "agents": {},
            "codex_settings": {"1_10": {"model": "gpt-5.4", "reasoning_effort": "high"}},
            "owners": {},
        }

        with mock.patch.object(telegram_bot, "save_state"):
            settings = telegram_bot._set_codex_settings((1, 10), state, clear=True)

        self.assertEqual(settings, {})
        self.assertEqual(telegram_bot._codex_settings_for((1, 10), state), {})

    def test_invalid_effort_is_ignored(self) -> None:
        state = {"offset": 0, "agents": {}, "codex_settings": {}, "owners": {}}

        with mock.patch.object(telegram_bot, "save_state"):
            settings = telegram_bot._set_codex_settings(
                (1, 10),
                state,
                model="gpt-5.4",
                reasoning_effort="turbo",
            )

        self.assertEqual(settings, {"model": "gpt-5.4"})


class LoginRoutingTest(unittest.TestCase):
    def test_login_provider_binds_lane_even_when_already_connected(self) -> None:
        class ConnectedProvider:
            label = "Codex"

            def check(self) -> tuple[bool, str]:
                return True, "ok"

        sent: list[str] = []
        bot = telegram_bot.Bot.__new__(telegram_bot.Bot)
        bot.state = {"offset": 0, "agents": {}, "codex_settings": {}, "owners": {}}
        bot.send = lambda _chat, text, **_kwargs: sent.append(text)  # type: ignore[method-assign]

        with mock.patch.object(telegram_bot, "save_state"):
            bot._start_login_provider("codex", ConnectedProvider(), 100, 55, 123)

        self.assertEqual(bot.state["agents"]["100_123"], "codex")
        self.assertIn("already connected", sent[0])

    def test_auth_and_quota_errors_trigger_login_picker_detection(self) -> None:
        self.assertTrue(
            telegram_bot._is_claude_auth_error(
                "Failed to authenticate. API Error: 401 authentication_error"
            )
        )
        self.assertTrue(telegram_bot._is_claude_auth_error("You are out of extra usage."))
        self.assertTrue(telegram_bot._is_codex_auth_error("usage limit reached"))

    def test_login_picker_codex_does_not_force_relogin(self) -> None:
        bot = telegram_bot.Bot.__new__(telegram_bot.Bot)
        bot.state = {
            "offset": 0,
            "agents": {},
            "codex_settings": {},
            "owners": {"100": {"user_id": "55", "name": "Magnus"}},
        }
        calls: list[tuple[str, dict]] = []

        def fake_call(method: str, **kwargs):
            calls.append((method, kwargs))
            return {"ok": True}

        bot.call = fake_call  # type: ignore[method-assign]
        with (
            mock.patch.object(telegram_bot, "_login_status_cache_invalidate"),
            mock.patch.object(bot, "_start_login_provider") as start_login,
        ):
            bot._handle_login_picker_callback(
                {
                    "id": "cb1",
                    "from": {"id": 55, "username": "Magnus_Mueller"},
                    "message": {
                        "chat": {"id": 100},
                        "message_id": 99,
                        "message_thread_id": 123,
                    },
                },
                "login_pick:codex",
            )

        _, kwargs = start_login.call_args
        self.assertNotIn("force", kwargs)
        self.assertTrue(kwargs["minimal_login_mode"])


class MiniAppLaunchTest(unittest.TestCase):
    def test_public_url_can_be_read_from_tg_env_when_process_env_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tg_env = Path(tmp) / "tg.env"
            tg_env.write_text(
                "TG_BOT_TOKEN=token\n"
                "BUX_MINIAPP_PUBLIC_URL=https://stable.trycloudflare.com\n",
                encoding="utf-8",
            )
            with mock.patch.object(telegram_bot, "TG_ENV", tg_env):
                old = os.environ.pop("BUX_MINIAPP_PUBLIC_URL", None)
                try:
                    self.assertEqual(
                        telegram_bot._miniapp_public_url_from_env(),
                        "https://stable.trycloudflare.com",
                    )
                finally:
                    if old is not None:
                        os.environ["BUX_MINIAPP_PUBLIC_URL"] = old

    def test_public_url_can_be_read_from_tunnel_url_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tg_env = Path(tmp) / "tg.env"
            tunnel_url = Path(tmp) / "url"
            tunnel_url.write_text("https://file.trycloudflare.com\n", encoding="utf-8")
            with (
                mock.patch.object(telegram_bot, "TG_ENV", tg_env),
                mock.patch.object(telegram_bot, "MINIAPP_TUNNEL_URL_FILE", tunnel_url),
            ):
                old = os.environ.pop("BUX_MINIAPP_PUBLIC_URL", None)
                try:
                    self.assertEqual(
                        telegram_bot._miniapp_public_url_from_env(),
                        "https://file.trycloudflare.com",
                    )
                finally:
                    if old is not None:
                        os.environ["BUX_MINIAPP_PUBLIC_URL"] = old


class AgencyButtonPromptTest(unittest.TestCase):
    def test_custom_button_prompt_includes_card_context(self) -> None:
        prompt = telegram_bot._agency_build_custom_dispatch_prompt(
            "✏️ Show 3 variants",
            {"username": "Magnus_Mueller"},
            {
                "id": 851,
                "title": "Repost Saurav's n8n launch",
                "source": "slack-wall-channel-teammate-direct-repost-ask",
                "tg_thread_id": 3280,
                "tg_message_id": 99,
                "buttons_json": json.dumps(
                    ["🟢 QT - A1 default", "✏️ Show 3 variants", "❌ Skip"]
                ),
                "description": "Slack signal: Saurav asked for reposts.",
                "prompt": "",
            },
        )

        self.assertIn("[agency-button] ✏️ Show 3 variants", prompt)
        self.assertIn("Title: Repost Saurav's n8n launch", prompt)
        self.assertIn("Source: slack-wall-channel-teammate-direct-repost-ask", prompt)
        self.assertIn("Buttons shown: 🟢 QT - A1 default | ✏️ Show 3 variants | ❌ Skip", prompt)
        self.assertIn("Slack signal: Saurav asked for reposts.", prompt)
        self.assertIn("find the matching entry by source or title", prompt)

    def test_legacy_custom_button_prompt_still_works_without_row(self) -> None:
        prompt = telegram_bot._agency_build_custom_dispatch_prompt(
            "🔁 Redo", {"first_name": "Magnus"}, None
        )

        self.assertIn("[agency-button] 🔁 Redo (tapped by @Magnus)", prompt)
        self.assertIn("rethink this suggestion", prompt)


class GoalModeTest(unittest.TestCase):
    """Tests for the /autopilot + /copilot per-topic mode flow."""

    def setUp(self) -> None:
        # Isolated mini-app DB per test so writes don't bleed.
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._db_patch = mock.patch.object(
            telegram_bot, "MINIAPP_DB", Path(self._tmp.name)
        )
        self._db_patch.start()
        # Also isolate the goals.md file (the goal-recording path appends to it).
        self._goals_tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
        self._goals_tmp.close()
        self._goals_patch = mock.patch.object(
            telegram_bot, "GOALS_FILE", Path(self._goals_tmp.name)
        )
        self._goals_patch.start()

    def tearDown(self) -> None:
        self._db_patch.stop()
        self._goals_patch.stop()
        os.unlink(self._tmp.name)
        os.unlink(self._goals_tmp.name)

    def test_default_mode_is_copilot(self) -> None:
        # No goal row yet — fall back to default.
        self.assertEqual(telegram_bot._get_goal_mode(123, 99), "copilot")

    def test_record_then_get_mode(self) -> None:
        gid = telegram_bot._record_miniapp_goal(
            "ship demo", "context", "", 123, 99, mode="autopilot"
        )
        self.assertIsNotNone(gid)
        self.assertEqual(telegram_bot._get_goal_mode(123, 99), "autopilot")

    def test_invalid_mode_falls_back_to_default(self) -> None:
        # Junk mode -> stored as default.
        gid = telegram_bot._record_miniapp_goal(
            "x", "", "", 123, 99, mode="ludicrous-speed"
        )
        self.assertIsNotNone(gid)
        self.assertEqual(telegram_bot._get_goal_mode(123, 99), "copilot")

    def test_set_goal_mode_with_no_goal_returns_false(self) -> None:
        # /autopilot before /goal -> can't flip a nonexistent row.
        self.assertFalse(telegram_bot._set_goal_mode(123, 99, "autopilot"))

    def test_set_goal_mode_flips_existing_row(self) -> None:
        telegram_bot._record_miniapp_goal("x", "", "", 123, 99)  # default copilot
        self.assertEqual(telegram_bot._get_goal_mode(123, 99), "copilot")
        self.assertTrue(telegram_bot._set_goal_mode(123, 99, "autopilot"))
        self.assertEqual(telegram_bot._get_goal_mode(123, 99), "autopilot")

    def test_set_goal_mode_rejects_unknown_value(self) -> None:
        telegram_bot._record_miniapp_goal("x", "", "", 123, 99)
        self.assertFalse(telegram_bot._set_goal_mode(123, 99, "nonsense"))
        # Mode unchanged.
        self.assertEqual(telegram_bot._get_goal_mode(123, 99), "copilot")


class AgencyGoalPromptTest(unittest.TestCase):
    """The /goal cycle prompt must mention current mode + self-scheduling."""

    def test_copilot_prompt_mentions_drafting_and_asking(self) -> None:
        prompt = telegram_bot._agency_goal_prompt("100k views", "TikTok", mode="copilot")
        self.assertIn("copilot", prompt.lower())
        # Drafting/asking framing
        self.assertIn("ask", prompt.lower())
        # Self-schedule instruction
        self.assertIn("tg-schedule", prompt)
        # ONE concrete action per cycle
        self.assertIn("ONE", prompt)

    def test_autopilot_prompt_mentions_acting_directly(self) -> None:
        prompt = telegram_bot._agency_goal_prompt("100k views", "TikTok", mode="autopilot")
        self.assertIn("autopilot", prompt.lower())
        self.assertIn("act", prompt.lower())
        self.assertIn("tg-schedule", prompt)


if __name__ == "__main__":
    unittest.main()
