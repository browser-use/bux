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

    def test_codex_goal_feature_enablement_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            self.assertTrue(telegram_bot._ensure_codex_goal_feature_enabled(config))
            self.assertIn("[features]\ngoals = true", config.read_text())
            self.assertFalse(telegram_bot._ensure_codex_goal_feature_enabled(config))

    def test_codex_goal_feature_is_inserted_into_existing_features_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text("[features]\nother = true\n", encoding="utf-8")

            self.assertTrue(telegram_bot._ensure_codex_goal_feature_enabled(config))

            self.assertIn("[features]\ngoals = true\nother = true", config.read_text())


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


class GoalCommandRoutingTest(unittest.TestCase):
    def test_goal_command_starts_interactive_codex_session(self) -> None:
        bot = telegram_bot.Bot.__new__(telegram_bot.Bot)
        bot.state = {"offset": 0, "agents": {}, "codex_settings": {}, "owners": {}}
        bot.setup_token = None
        bot._username = "bux_bot"
        bot.react = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.typing = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

        sent_to_pty: list[str] = []

        class InstantThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        sess = mock.Mock()
        sess.alive = True
        sess.send_input.side_effect = lambda text: sent_to_pty.append(text) or True

        with (
            mock.patch.object(telegram_bot, "load_allow", return_value={100}),
            mock.patch.object(telegram_bot, "save_state"),
            mock.patch.object(telegram_bot, "_get_shell_session", return_value=None),
            mock.patch.object(telegram_bot, "ShellSession", return_value=sess) as shell_session,
            mock.patch.object(telegram_bot.threading, "Thread", InstantThread),
            mock.patch.object(telegram_bot.time, "sleep", return_value=None),
            mock.patch.object(
                telegram_bot,
                "_codex_interactive_command_for",
                return_value="codex --no-alt-screen resume thread",
            ),
            mock.patch.object(telegram_bot, "_ensure_codex_goal_feature_enabled") as ensure_goal,
        ):
            bot.handle(
                {
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 55, "username": "Magnus_Mueller"},
                    "message_id": 123,
                    "text": "/goal improve Agency UI",
                }
            )

        self.assertEqual(bot.state["agents"]["100_main"], "codex")
        ensure_goal.assert_called_once()
        shell_session.assert_called_once()
        self.assertEqual(
            shell_session.call_args.kwargs["initial_cmd"],
            "codex --no-alt-screen resume thread",
        )
        sess.start.assert_called_once()
        self.assertEqual(sent_to_pty, ["/goal improve Agency UI"])


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

    def test_plain_reply_context_includes_source_url(self) -> None:
        prompt = telegram_bot._agency_build_plain_reply_context(
            {
                "id": 1272,
                "title": "Post OpenClaw's Browser Harness guide",
                "source": "browser-openclaw-browser-harness-guide",
                "source_label": "OpenClaw guide",
                "source_url": "https://openclawlaunch.com/guides/openclaw-browser-harness",
                "description": "Third-party guide context.",
                "prompt": "Re-open the guide before posting.",
            },
            "who built this?",
        )

        self.assertIn("Suggestion id: 1272", prompt)
        self.assertIn("Post OpenClaw's Browser Harness guide", prompt)
        self.assertIn("https://openclawlaunch.com/guides/openclaw-browser-harness", prompt)
        self.assertIn("Re-open the guide before posting.", prompt)
        self.assertIn("User reply:\nwho built this?", prompt)


class UpdateRequestLanesTest(unittest.TestCase):
    """Parse + dispatch behavior for /var/lib/bux/update-request.lanes."""

    def test_legacy_two_column_rows_are_back_online(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "update-request.lanes"
            f.write_text("100\t\n200\t300\n", encoding="utf-8")
            with mock.patch.object(telegram_bot, "UPDATE_REQUEST_LANES", f):
                got = telegram_bot._consume_update_request_lanes()
            self.assertEqual(
                got,
                {
                    (100, 0): telegram_bot.UPDATE_REQUEST_KIND_BACK_ONLINE,
                    (200, 300): telegram_bot.UPDATE_REQUEST_KIND_BACK_ONLINE,
                },
            )
            self.assertFalse(f.exists(), "file should be consumed (unlinked)")

    def test_three_column_self_restart_kind_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "update-request.lanes"
            f.write_text("100\t300\tself-restart\n", encoding="utf-8")
            with mock.patch.object(telegram_bot, "UPDATE_REQUEST_LANES", f):
                got = telegram_bot._consume_update_request_lanes()
            self.assertEqual(
                got,
                {(100, 300): telegram_bot.UPDATE_REQUEST_KIND_SELF_RESTART},
            )

    def test_self_restart_wins_when_lane_appears_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "update-request.lanes"
            f.write_text(
                "100\t300\n100\t300\tself-restart\n",
                encoding="utf-8",
            )
            with mock.patch.object(telegram_bot, "UPDATE_REQUEST_LANES", f):
                got = telegram_bot._consume_update_request_lanes()
            self.assertEqual(
                got[(100, 300)],
                telegram_bot.UPDATE_REQUEST_KIND_SELF_RESTART,
            )

    def test_unknown_kind_falls_back_to_back_online(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "update-request.lanes"
            f.write_text("100\t300\tbogus\n", encoding="utf-8")
            with mock.patch.object(telegram_bot, "UPDATE_REQUEST_LANES", f):
                got = telegram_bot._consume_update_request_lanes()
            self.assertEqual(
                got[(100, 300)],
                telegram_bot.UPDATE_REQUEST_KIND_BACK_ONLINE,
            )

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "update-request.lanes"
            with mock.patch.object(telegram_bot, "UPDATE_REQUEST_LANES", f):
                self.assertEqual(telegram_bot._consume_update_request_lanes(), {})


class SelfRestartContinuationTest(unittest.TestCase):
    """Continuation turn is enqueued into the same lane, front of queue."""

    def setUp(self) -> None:
        # Reset module-level lane state — these tests poke it directly.
        telegram_bot._lanes.clear()
        telegram_bot._lane_workers.clear()

    def test_enqueues_one_job_per_lane_at_front_of_queue(self) -> None:
        bot = mock.MagicMock()
        # Pre-existing user follow-up that arrived during the restart window.
        slug = telegram_bot._lane_slug((100, 300))
        existing_job = {
            "id": "deadbeef",
            "chat_id": 100,
            "thread_id": 300,
            "prompt": "user follow-up sent while box was down",
            "queued_at": 1.0,
            "status": "queued",
        }
        with mock.patch.object(telegram_bot, "_save_lanes_to_disk_locked"):
            telegram_bot._lanes[slug] = [existing_job]
            enqueued = telegram_bot._enqueue_self_restart_continuations(
                bot, [(100, 300)], sha="abc123", branch="main"
            )

        self.assertEqual(enqueued, {(100, 300)})
        lane = telegram_bot._lanes[slug]
        self.assertEqual(len(lane), 2)
        # Continuation runs first; user follow-up stays queued behind it.
        self.assertEqual(lane[0]["status"], "queued")
        self.assertIn("bux-restart finished", lane[0]["prompt"])
        self.assertIn("abc123", lane[0]["prompt"])
        self.assertIn("main", lane[0]["prompt"])
        self.assertIsNone(lane[0]["sender"])
        self.assertEqual(lane[1]["id"], "deadbeef")

    def test_skips_lane_when_enqueue_fails(self) -> None:
        bot = mock.MagicMock()
        with mock.patch.object(telegram_bot, "_enqueue", side_effect=RuntimeError("boom")):
            enqueued = telegram_bot._enqueue_self_restart_continuations(
                bot, [(100, 300), (400, 0)], sha="abc", branch="main"
            )
        self.assertEqual(enqueued, set())


if __name__ == "__main__":
    unittest.main()
