from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
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
                service_tier="fast",
            )

        self.assertEqual(
            telegram_bot._codex_settings_for(first, state),
            {"model": "gpt-5.4-mini", "reasoning_effort": "low", "service_tier": "fast"},
        )
        self.assertEqual(telegram_bot._codex_settings_for(second, state), telegram_bot.CODEX_DEFAULT_SETTINGS)

    def test_clear_codex_settings(self) -> None:
        state = {
            "offset": 0,
            "agents": {},
            "codex_settings": {"1_10": {"model": "gpt-5.4", "reasoning_effort": "high"}},
            "owners": {},
        }

        with mock.patch.object(telegram_bot, "save_state"):
            settings = telegram_bot._set_codex_settings((1, 10), state, clear=True)

        self.assertEqual(settings, telegram_bot.CODEX_DEFAULT_SETTINGS)
        self.assertEqual(
            telegram_bot._codex_settings_for((1, 10), state),
            telegram_bot.CODEX_DEFAULT_SETTINGS,
        )

    def test_invalid_effort_is_ignored(self) -> None:
        state = {"offset": 0, "agents": {}, "codex_settings": {}, "owners": {}}

        with mock.patch.object(telegram_bot, "save_state"):
            settings = telegram_bot._set_codex_settings(
                (1, 10),
                state,
                model="gpt-5.4",
                reasoning_effort="turbo",
            )

        self.assertEqual(
            settings,
            {"model": "gpt-5.4", "reasoning_effort": "xhigh", "service_tier": "fast"},
        )

    def test_invalid_service_tier_is_ignored(self) -> None:
        state = {"offset": 0, "agents": {}, "codex_settings": {}, "owners": {}}

        with mock.patch.object(telegram_bot, "save_state"):
            settings = telegram_bot._set_codex_settings(
                (1, 10),
                state,
                service_tier="turbo",
            )

        self.assertEqual(settings, telegram_bot.CODEX_DEFAULT_SETTINGS)

    def test_legacy_priority_service_tier_is_normalized(self) -> None:
        state = {
            "offset": 0,
            "agents": {},
            "codex_settings": {"1_10": {"service_tier": "priority"}},
            "owners": {},
        }

        self.assertEqual(
            telegram_bot._codex_settings_for((1, 10), state),
            {"model": "gpt-5.5", "reasoning_effort": "xhigh", "service_tier": "fast"},
        )

    def test_model_picker_marks_current_choices(self) -> None:
        markup = telegram_bot._codex_model_picker_markup(
            {"model": "gpt-5.4", "reasoning_effort": "high", "service_tier": "fast"}
        )

        labels = [
            button["text"]
            for row in markup["inline_keyboard"]
            for button in row
        ]
        self.assertIn("✓ 5.4", labels)
        self.assertIn("✓ High", labels)
        self.assertIn("Fast mode: on", labels)
        self.assertIn("Reset Codex defaults", labels)

    def test_model_picker_callback_updates_effort_in_place(self) -> None:
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
        bot.send = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

        with mock.patch.object(telegram_bot, "save_state"):
            bot._handle_codex_model_callback(
                {
                    "id": "cb1",
                    "from": {"id": 55, "username": "Magnus_Mueller"},
                    "message": {
                        "chat": {"id": 100},
                        "message_id": 99,
                        "message_thread_id": 123,
                    },
                },
                "codex_model:effort:high",
            )

        self.assertEqual(
            bot.state["codex_settings"]["100_123"],
            {"reasoning_effort": "high"},
        )
        self.assertEqual(bot.state["agents"]["100_123"], "codex")
        self.assertTrue(any(method == "editMessageText" for method, _ in calls))

    def test_fast_callback_toggles_service_tier(self) -> None:
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
        bot.send = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

        with mock.patch.object(telegram_bot, "save_state"):
            bot._handle_codex_model_callback(
                {
                    "id": "cb1",
                    "from": {"id": 55, "username": "Magnus_Mueller"},
                    "message": {
                        "chat": {"id": 100},
                        "message_id": 99,
                        "message_thread_id": 123,
                    },
                },
                "codex_model:fast:toggle",
            )

        self.assertEqual(
            bot.state["codex_settings"]["100_123"],
            {"service_tier": "off"},
        )
        self.assertTrue(any(method == "editMessageText" for method, _ in calls))

    def test_plain_fast_toggles_default_fast_service_tier_off(self) -> None:
        sent: list[tuple[str, dict]] = []
        bot = telegram_bot.Bot.__new__(telegram_bot.Bot)
        bot.state = {"offset": 0, "agents": {}, "codex_settings": {}, "owners": {}}
        bot.setup_token = None
        bot._username = "bux_bot"
        bot.react = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.typing = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.send = lambda _chat, text, **kwargs: sent.append((text, kwargs))  # type: ignore[method-assign]

        with (
            mock.patch.object(telegram_bot, "load_allow", return_value={100}),
            mock.patch.object(telegram_bot, "save_state"),
            mock.patch.object(telegram_bot, "_get_shell_session", return_value=None),
            mock.patch.object(telegram_bot, "_goal_state_for", return_value=None),
        ):
            bot.handle(
                {
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 55, "username": "Magnus_Mueller"},
                    "message_id": 123,
                    "text": "fast",
                }
            )

        self.assertEqual(bot.state["agents"]["100_main"], "codex")
        self.assertEqual(
            bot.state["codex_settings"]["100_main"],
            {"service_tier": "off"},
        )
        self.assertIn("Fast mode off.", sent[-1][0])
        self.assertNotIn("reply_markup", sent[-1][1])

    def test_plain_fast_toggles_fast_service_tier_on(self) -> None:
        sent: list[tuple[str, dict]] = []
        bot = telegram_bot.Bot.__new__(telegram_bot.Bot)
        bot.state = {
            "offset": 0,
            "agents": {},
            "codex_settings": {"100_main": {"service_tier": "off", "reasoning_effort": "xhigh"}},
            "owners": {},
        }
        bot.setup_token = None
        bot._username = "bux_bot"
        bot.react = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.typing = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.send = lambda _chat, text, **kwargs: sent.append((text, kwargs))  # type: ignore[method-assign]

        with (
            mock.patch.object(telegram_bot, "load_allow", return_value={100}),
            mock.patch.object(telegram_bot, "save_state"),
            mock.patch.object(telegram_bot, "_get_shell_session", return_value=None),
            mock.patch.object(telegram_bot, "_goal_state_for", return_value=None),
        ):
            bot.handle(
                {
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 55, "username": "Magnus_Mueller"},
                    "message_id": 123,
                    "text": "fast",
                }
            )

        self.assertEqual(
            bot.state["codex_settings"]["100_main"],
            {"service_tier": "fast", "reasoning_effort": "xhigh"},
        )
        self.assertIn("Fast mode on.", sent[-1][0])

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
    def test_goal_command_starts_durable_codex_goal_session(self) -> None:
        bot = telegram_bot.Bot.__new__(telegram_bot.Bot)
        bot.state = {"offset": 0, "agents": {}, "codex_settings": {}, "owners": {}, "goal_tmux": {}}
        bot.setup_token = None
        bot._username = "bux_bot"
        bot.react = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.typing = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.send = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

        with (
            mock.patch.object(telegram_bot, "load_allow", return_value={100}),
            mock.patch.object(telegram_bot, "save_state"),
            mock.patch.object(telegram_bot, "_get_shell_session", return_value=None),
            mock.patch.object(telegram_bot, "_goal_state_for", return_value=None),
            mock.patch.object(telegram_bot, "_start_goal_tmux") as start_goal,
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
        start_goal.assert_called_once()
        self.assertEqual(start_goal.call_args.args[-1], "/goal improve Agency UI")

    def test_goal_command_forwards_nested_slash_command_to_existing_goal_session(self) -> None:
        bot = telegram_bot.Bot.__new__(telegram_bot.Bot)
        bot.state = {
            "offset": 0,
            "agents": {},
            "codex_settings": {},
            "owners": {},
            "goal_tmux": {"100_main": {"name": "goal-session", "chat_id": 100, "thread_id": 0}},
        }
        bot.setup_token = None
        bot._username = "bux_bot"
        sent: list[str] = []
        bot.react = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.typing = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.send = lambda _chat_id, text, **_kwargs: sent.append(text)  # type: ignore[method-assign]

        with (
            mock.patch.object(telegram_bot, "load_allow", return_value={100}),
            mock.patch.object(telegram_bot, "save_state"),
            mock.patch.object(telegram_bot, "_goal_tmux_alive", return_value=True),
            mock.patch.object(telegram_bot, "_ensure_goal_relay", return_value=True),
            mock.patch.object(telegram_bot, "_send_goal_tmux_input", return_value=True) as send_input,
            mock.patch.object(telegram_bot, "_ensure_codex_goal_feature_enabled"),
        ):
            bot.handle(
                {
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 55, "username": "Magnus_Mueller"},
                    "message_id": 124,
                    "text": "/goal count to 12",
                }
            )

        send_input.assert_called_once()
        self.assertEqual(send_input.call_args.args[0], "goal-session")
        self.assertEqual(send_input.call_args.args[1], "/goal count to 12")
        self.assertIn("sent goal to the live Codex goal session", sent[-1])

    def test_goal_relay_sends_codex_final_answer_events(self) -> None:
        sent: list[tuple[int, str, dict]] = []
        edits: list[tuple[int, int, str, dict]] = []

        class FakeBot:
            def __init__(self) -> None:
                self.state = {"goal_tmux": {}}

            def send(self, chat_id: int, text: str, **kwargs) -> None:
                sent.append((chat_id, text, kwargs))

            def send_returning_id(self, chat_id: int, text: str, **kwargs) -> int:
                sent.append((chat_id, text, kwargs))
                return 777

            def edit(self, chat_id: int, message_id: int, text: str, **kwargs) -> bool:
                edits.append((chat_id, message_id, text, kwargs))
                return True

        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "final_answer",
                            "message": "1 2 3 4 5 6 7 8 9 10",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            relay = telegram_bot.GoalTmuxRelay(FakeBot(), "slug", 100, 123, "tmux-name")
            relay._rollout_path = rollout
            relay._rollout_pos = 0

            relay._relay_rollout_events()

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 100)
        self.assertEqual(sent[0][2]["thread_id"], 123)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0][0], 100)
        self.assertEqual(edits[0][1], 777)
        self.assertIn("1 2 3 4 5 6 7 8 9 10", edits[0][2])

    def test_goal_relay_keeps_goal_mode_after_final_answer_without_queued_input(self) -> None:
        sent: list[tuple[int, str, dict]] = []
        edits: list[tuple[int, int, str, dict]] = []

        class FakeBot:
            def __init__(self) -> None:
                self.state = {
                    "goal_tmux": {"slug": {"name": "tmux-name", "chat_id": 100, "thread_id": 123}}
                }

            def send(self, chat_id: int, text: str, **kwargs) -> None:
                sent.append((chat_id, text, kwargs))

            def send_returning_id(self, chat_id: int, text: str, **kwargs) -> int:
                sent.append((chat_id, text, kwargs))
                return 777

            def edit(self, chat_id: int, message_id: int, text: str, **kwargs) -> bool:
                edits.append((chat_id, message_id, text, kwargs))
                return True

        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "final_answer",
                            "message": "1 2 3",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            bot = FakeBot()
            relay = telegram_bot.GoalTmuxRelay(bot, "slug", 100, 123, "tmux-name")
            relay._rollout_path = rollout
            relay._rollout_pos = 0

            with mock.patch.object(telegram_bot, "_run_tmux") as run_tmux:
                relay._relay_rollout_events()

        self.assertIn("slug", bot.state["goal_tmux"])
        run_tmux.assert_not_called()
        self.assertIn("1 2 3", edits[-1][2])
        self.assertIn("Time used:", edits[-1][2])

    def test_goal_relay_sends_queued_followup_after_current_final_answer(self) -> None:
        sent: list[tuple[int, str, dict]] = []
        edits: list[tuple[int, int, str, dict]] = []

        class FakeBot:
            def __init__(self) -> None:
                self.state = {
                    "goal_tmux": {"slug": {"name": "tmux-name", "chat_id": 100, "thread_id": 123}}
                }

            def send(self, chat_id: int, text: str, **kwargs) -> None:
                sent.append((chat_id, text, kwargs))

            def send_returning_id(self, chat_id: int, text: str, **kwargs) -> int:
                sent.append((chat_id, text, kwargs))
                return 777

            def edit(self, chat_id: int, message_id: int, text: str, **kwargs) -> bool:
                edits.append((chat_id, message_id, text, kwargs))
                return True

        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "final_answer",
                            "message": "1 2 3 4 5",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            relay = telegram_bot.GoalTmuxRelay(FakeBot(), "slug", 100, 123, "tmux-name")
            relay._rollout_path = rollout
            relay._rollout_pos = 0
            self.assertTrue(relay.queue_if_busy("what's the time?"))

            with mock.patch.object(telegram_bot, "_send_goal_tmux_input", return_value=True) as send_input:
                relay._relay_rollout_events()

        send_input.assert_called_once_with(
            "tmux-name",
            "what's the time?",
            slug="slug",
            queue_if_busy=False,
        )
        self.assertIn("1 2 3 4 5", edits[-1][2])
        self.assertIn("Time used:", edits[-1][2])

    def test_goal_relay_does_not_duplicate_existing_time_used_footer(self) -> None:
        sent: list[tuple[int, str, dict]] = []
        edits: list[tuple[int, int, str, dict]] = []

        class FakeBot:
            def __init__(self) -> None:
                self.state = {"goal_tmux": {}}

            def send(self, chat_id: int, text: str, **kwargs) -> None:
                sent.append((chat_id, text, kwargs))

            def send_returning_id(self, chat_id: int, text: str, **kwargs) -> int:
                sent.append((chat_id, text, kwargs))
                return 777

            def edit(self, chat_id: int, message_id: int, text: str, **kwargs) -> bool:
                edits.append((chat_id, message_id, text, kwargs))
                return True

        relay = telegram_bot.GoalTmuxRelay(FakeBot(), "slug", 100, 123, "tmux-name")
        relay._relay_message(relay._with_elapsed_footer("Done.\n\nTime used: 7 seconds."), final=True)

        self.assertEqual(edits[-1][2].count("Time used:"), 1)

    def test_goal_relay_sends_heartbeat_while_waiting_for_output(self) -> None:
        sent: list[tuple[int, str, dict]] = []
        edits: list[tuple[int, int, str, dict]] = []

        class FakeBot:
            def __init__(self) -> None:
                self.state = {"goal_tmux": {"slug": {"name": "tmux-name"}}}

            def send(self, chat_id: int, text: str, **kwargs) -> None:
                sent.append((chat_id, text, kwargs))

            def send_returning_id(self, chat_id: int, text: str, **kwargs) -> int:
                sent.append((chat_id, text, kwargs))
                return 777

            def edit(self, chat_id: int, message_id: int, text: str, **kwargs) -> bool:
                edits.append((chat_id, message_id, text, kwargs))
                return True

        relay = telegram_bot.GoalTmuxRelay(FakeBot(), "slug", 100, 123, "tmux-name")
        relay._turn_started_at = time.monotonic() - 12
        relay._next_heartbeat_at = 0
        relay._pending_inputs.append("what's the time?")

        relay._maybe_relay_heartbeat()

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 100)
        rendered = edits[-1][2] if edits else sent[-1][1]
        self.assertIn("Codex goal is still working", rendered)
        self.assertIn("Queued follow", rendered)
        self.assertIn("1", rendered)

    def test_goal_tmux_input_confirms_nested_goal_prompt(self) -> None:
        calls: list[list[str]] = []

        def fake_run_tmux(args: list[str], timeout: float) -> mock.Mock:
            calls.append(args)
            result = mock.Mock()
            result.returncode = 0
            return result

        with (
            mock.patch.object(telegram_bot, "_run_tmux", side_effect=fake_run_tmux),
            mock.patch.object(telegram_bot.time, "sleep"),
        ):
            ok = telegram_bot._send_goal_tmux_input("tmux-name", "/goal count to 10")

        self.assertTrue(ok)
        self.assertEqual(
            calls,
            [
                ["send-keys", "-t", "tmux-name", "-l", "/goal count to 10"],
                ["send-keys", "-t", "tmux-name", "Enter"],
                ["send-keys", "-t", "tmux-name", "Enter"],
            ],
        )

    def test_goal_relay_reuses_one_editable_message_within_one_turn(self) -> None:
        sent: list[tuple[int, str, dict]] = []
        edits: list[tuple[int, int, str, dict]] = []

        class FakeBot:
            def __init__(self) -> None:
                self.state = {"goal_tmux": {}}

            def send(self, chat_id: int, text: str, **kwargs) -> None:
                sent.append((chat_id, text, kwargs))

            def send_returning_id(self, chat_id: int, text: str, **kwargs) -> int:
                sent.append((chat_id, text, kwargs))
                return 777

            def edit(self, chat_id: int, message_id: int, text: str, **kwargs) -> bool:
                edits.append((chat_id, message_id, text, kwargs))
                return True

        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            events = [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "1 2 3 4 5 6 7 8 9 10",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "Yes.",
                    },
                },
            ]
            rollout.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            relay = telegram_bot.GoalTmuxRelay(FakeBot(), "slug", 100, 123, "tmux-name")
            relay._rollout_path = rollout
            relay._rollout_pos = 0

            relay._relay_rollout_events()

        self.assertEqual(len(sent), 1)
        self.assertGreaterEqual(len(edits), 2)
        self.assertEqual({edit[1] for edit in edits}, {777})
        self.assertIn("Yes", edits[-1][2])
        self.assertIn("1 2 3 4 5 6 7 8 9 10", edits[-1][2])

    def test_goal_relay_starts_new_editable_message_after_user_input(self) -> None:
        sent: list[tuple[int, str, dict]] = []
        edits: list[tuple[int, int, str, dict]] = []
        next_id = 776

        class FakeBot:
            def send(self, chat_id: int, text: str, **kwargs) -> None:
                sent.append((chat_id, text, kwargs))

            def send_returning_id(self, chat_id: int, text: str, **kwargs) -> int:
                nonlocal next_id
                next_id += 1
                sent.append((chat_id, text, kwargs))
                return next_id

            def edit(self, chat_id: int, message_id: int, text: str, **kwargs) -> bool:
                edits.append((chat_id, message_id, text, kwargs))
                return True

        relay = telegram_bot.GoalTmuxRelay(FakeBot(), "slug", 100, 123, "tmux-name")

        relay._relay_message("first answer", final=True)
        relay.begin_user_turn()
        relay._relay_message("second answer", final=True)

        self.assertEqual(len(sent), 2)
        self.assertEqual([edit[1] for edit in edits], [777, 778])
        self.assertIn("first answer", edits[0][2])
        self.assertIn("second answer", edits[1][2])
        self.assertNotIn("first answer", edits[1][2])

    def test_goal_start_message_mentions_cancel(self) -> None:
        sent: list[str] = []
        bot = telegram_bot.Bot.__new__(telegram_bot.Bot)
        bot.state = {"offset": 0, "agents": {}, "codex_settings": {}, "owners": {}, "goal_tmux": {}}
        bot.setup_token = None
        bot._username = "bux_bot"
        bot.react = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.typing = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.send = lambda _chat, text, **_kwargs: sent.append(text)  # type: ignore[method-assign]

        with (
            mock.patch.object(telegram_bot, "load_allow", return_value={100}),
            mock.patch.object(telegram_bot, "save_state"),
            mock.patch.object(telegram_bot, "_get_shell_session", return_value=None),
            mock.patch.object(telegram_bot, "_goal_state_for", return_value=None),
            mock.patch.object(telegram_bot, "_start_goal_tmux"),
            mock.patch.object(telegram_bot, "_ensure_codex_goal_feature_enabled"),
        ):
            bot.handle(
                {
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 55, "username": "Magnus_Mueller"},
                    "message_id": 123,
                    "text": "/goal improve Agency UI",
                }
            )

        self.assertIn("Use `/cancel` to stop it.", sent[-1])

    def test_goal_stop_kills_goal_session_and_clears_state(self) -> None:
        sent: list[str] = []
        bot = telegram_bot.Bot.__new__(telegram_bot.Bot)
        bot.state = {
            "offset": 0,
            "agents": {},
            "codex_settings": {},
            "owners": {},
            "goal_tmux": {"100_main": {"name": "goal-session", "chat_id": 100, "thread_id": 0}},
        }
        bot.setup_token = None
        bot._username = "bux_bot"
        bot.react = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.typing = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.send = lambda _chat, text, **_kwargs: sent.append(text)  # type: ignore[method-assign]

        with (
            mock.patch.object(telegram_bot, "load_allow", return_value={100}),
            mock.patch.object(telegram_bot, "save_state"),
            mock.patch.object(telegram_bot, "_goal_tmux_alive", return_value=True),
            mock.patch.object(telegram_bot, "_run_tmux") as run_tmux,
        ):
            run_tmux.return_value.returncode = 0
            bot.handle(
                {
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 55, "username": "Magnus_Mueller"},
                    "message_id": 123,
                    "text": "/goal stop",
                }
            )

        run_tmux.assert_called_once_with(["kill-session", "-t", "goal-session"], timeout=3.0)
        self.assertEqual(bot.state["goal_tmux"], {})
        self.assertIn("stopped the live Codex goal session", sent[-1])

    def test_plain_followup_waits_for_in_progress_goal_start(self) -> None:
        bot = telegram_bot.Bot.__new__(telegram_bot.Bot)
        bot.state = {
            "offset": 0,
            "agents": {},
            "codex_settings": {},
            "owners": {"100": {"user_id": "55", "name": "Magnus"}},
            "goal_tmux": {},
        }
        bot.setup_token = None
        bot._username = "bux_bot"
        bot.react = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.typing = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        bot.send = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        slug = "100_main"
        start_ev = telegram_bot._begin_goal_start(slug)

        def finish_goal_start() -> None:
            time.sleep(0.02)
            bot.state["goal_tmux"][slug] = {
                "name": "goal-session",
                "chat_id": 100,
                "thread_id": 0,
            }
            telegram_bot._finish_goal_start(slug, start_ev)

        finisher = threading.Thread(target=finish_goal_start)
        finisher.start()
        try:
            with (
                mock.patch.object(telegram_bot, "load_allow", return_value={100}),
                mock.patch.object(telegram_bot, "_goal_tmux_alive", return_value=True),
                mock.patch.object(telegram_bot, "_ensure_goal_relay", return_value=True),
                mock.patch.object(telegram_bot, "_send_goal_tmux_input", return_value=True) as send_input,
                mock.patch.object(telegram_bot, "_enqueue") as enqueue,
            ):
                bot.handle(
                    {
                        "chat": {"id": 100, "type": "private"},
                        "from": {"id": 55, "username": "Magnus_Mueller"},
                        "message_id": 123,
                        "text": "What's the time?",
                    }
                )
        finally:
            telegram_bot._finish_goal_start(slug, start_ev)
            finisher.join(timeout=1)

        send_input.assert_called_once_with("goal-session", "What's the time?", slug=slug)
        enqueue.assert_not_called()


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
