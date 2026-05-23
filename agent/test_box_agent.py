import unittest
import sys
import types
from unittest import mock

sys.modules.setdefault('websockets', types.ModuleType('websockets'))

from agent import box_agent


class ShellSessionLaunchTest(unittest.TestCase):
    def test_codex_launch_seeds_tmux_window_with_codex_cli(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **_kwargs: object) -> mock.Mock:
            calls.append(args)
            # First call is has-session: report missing so creation path runs.
            return mock.Mock(returncode=1 if len(calls) == 1 else 0)

        with (
            mock.patch.object(box_agent, 'CODEX_BIN', '/usr/local/bin/codex'),
            mock.patch('subprocess.run', side_effect=fake_run),
        ):
            box_agent.ShellSession._ensure_tmux_window(
                'bux-w1',
                launch='codex',
                dsp_enabled=True,
            )

        self.assertEqual(calls[0], ['/usr/bin/tmux', 'has-session', '-t', 'bux-w1'])
        self.assertEqual(calls[1][-3:], ['/bin/bash', '-lc', '/usr/local/bin/codex; exec bash -l'])
        self.assertEqual(calls[2], ['/usr/bin/tmux', 'set-window-option', '-t', 'bux-w1', 'aggressive-resize', 'on'])


if __name__ == '__main__':
    unittest.main()
