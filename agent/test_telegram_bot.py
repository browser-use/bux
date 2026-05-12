from __future__ import annotations

import sys
import unittest
from unittest import mock
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
