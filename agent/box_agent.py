"""box-agent — runs on every bux host as a systemd service.

Responsibilities:
  - Maintain outbound WebSocket to the cloud control plane.
  - Heartbeat every 30s.
  - Poll `claude auth status` (1s first minute, then 15s); push `claude_authed`
    to cloud on flip. Claude login itself is done by the USER inside the ttyd
    web terminal (they type /login), not driven from here — the UI changes too
    often to automate reliably.
  - Handle commands pushed from cloud:
      - run_task {prompt}         → `claude -p "<prompt>"`, stream stdout
      - shell_attach / shell_input / shell_resize / shell_close — web terminal
      - tg_install {bot_token}    → write /etc/bux/tg.env, start bux-tg service
      - ping                      → reply pong
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import select
import signal
import sys
import termios
import time
import uuid
from pathlib import Path

import websockets

LOG = logging.getLogger('box-agent')
ENV_PATH = Path('/etc/bux/env')
HEARTBEAT_INTERVAL = 30
TG_ENV = Path('/etc/bux/tg.env')

# Where the OSS repo is cloned by install.sh at bake time. /opt/bux/agent is
# a symlink to /opt/bux/repo/agent so systemd units' ExecStart=/opt/bux/agent
# paths resolve through the symlink. Updates run `git pull` here.
REPO_DIR = Path('/opt/bux/repo')


def _get_agent_sha() -> str:
	"""Short git SHA of the cloned agent repo, or 'unknown' off-repo (e.g.
	dev runs from a tarball). Surfaced in the hello payload so the cloud
	can show "running v0.4.2 (a3f9c1d)" in /bux."""
	try:
		import subprocess

		out = subprocess.run(
			['git', '-C', str(REPO_DIR), 'rev-parse', '--short', 'HEAD'],
			capture_output=True,
			text=True,
			timeout=3,
		)
		if out.returncode == 0:
			return out.stdout.strip()
	except Exception:
		pass
	return 'unknown'


def _get_agent_branch() -> str:
	"""Branch the agent repo is currently tracking. Lets the cloud surface
	"on stable" vs "on main" so users know whether they're pinned."""
	try:
		import subprocess

		out = subprocess.run(
			['git', '-C', str(REPO_DIR), 'rev-parse', '--abbrev-ref', 'HEAD'],
			capture_output=True,
			text=True,
			timeout=3,
		)
		if out.returncode == 0:
			return out.stdout.strip()
	except Exception:
		pass
	return 'unknown'


def load_env() -> dict[str, str]:
	"""Parse /etc/bux/env written by user-data at first boot.

	Returns dict of KEY=VAL pairs; missing file → empty dict.
	"""
	if not ENV_PATH.exists():
		return {}
	out: dict[str, str] = {}
	for line in ENV_PATH.read_text().splitlines():
		line = line.strip()
		if not line or line.startswith('#') or '=' not in line:
			continue
		k, v = line.split('=', 1)
		out[k.strip()] = v.strip().strip('"').strip("'")
	return out


def _read_tg_bot_username() -> str | None:
	"""Read TG_BOT_USERNAME from /etc/bux/tg.env if present.

	The file is root:bux 0640; box-agent runs as bux which is in the bux
	group, so the read works without sudo. Returns None when bux-tg
	isn't set up yet (no tg.env), or when the file exists but doesn't
	carry TG_BOT_USERNAME (older installs that pre-date this field —
	the user can re-Set up Telegram to populate it).

	Surfaced in the hello payload so the cloud can backfill its
	tg_bot_username column for boxes provisioned before
	install_telegram() started persisting it server-side.
	"""
	if not TG_ENV.exists():
		return None
	try:
		for line in TG_ENV.read_text().splitlines():
			line = line.strip()
			if not line or line.startswith('#') or '=' not in line:
				continue
			k, v = line.split('=', 1)
			if k.strip() == 'TG_BOT_USERNAME':
				val = v.strip().strip('"').strip("'")
				return val or None
	except Exception:
		# Permission flake / partial write during a concurrent install —
		# treat as "no handle yet" and the next reconnect will retry.
		LOG.exception('failed to read %s', TG_ENV)
	return None


# --- Claude auth ------------------------------------------------------------
#
# The user does the OAuth flow themselves inside the ttyd web terminal
# (`/login` inside claude). The agent just watches — an auth-poll loop
# shells out to `claude auth status` every 1s (first minute) or 15s (after)
# and pushes `claude_authed` over WS the moment it flips to loggedIn.

CLAUDE_BIN = '/usr/bin/claude'


def _find_codex_bin() -> str:
	"""Locate the codex CLI. Mirrors the lookup paths bux's installer
	bakes in (npm global → /usr/local → /usr/bin) so a freshly-installed
	box and a long-lived one with custom paths both resolve.
	Falls back to bare 'codex' (PATH lookup) if nothing exists yet — the
	auth_poll then surfaces "command not found" cleanly."""
	for p in (
		'/home/bux/.npm-global/bin/codex',
		'/usr/local/bin/codex',
		'/usr/bin/codex',
	):
		if os.path.exists(p):
			return p
	return 'codex'


CODEX_BIN = _find_codex_bin()
# Written by browser-keeper.service on each rotation. Source of truth for
# BU_BROWSER_ID + BU_CDP_WS + BU_BROWSER_LIVE_URL on the box.
BROWSER_ENV_PATH = '/home/bux/.claude/browser.env'


def _read_with_timeout(fd: int, max_bytes: int, timeout_seconds: float) -> bytes | None:
	"""Read up to `max_bytes` from `fd`, blocking up to `timeout_seconds`.

	Returns the bytes read (could be empty if pty hung up), or None on
	timeout. Raises OSError if the fd is closed / EOF (caller treats
	that as the pty exiting).

	Used by the claude_login pty reader loop. We use select() rather
	than non-blocking reads so we can sleep cheaply between chunks
	without burning CPU.
	"""
	import os as _os
	import select

	r, _, _ = select.select([fd], [], [], timeout_seconds)
	if not r:
		return None
	# Could raise OSError if the fd is closed by the time we read.
	# Caller treats that as pty exit.
	data = _os.read(fd, max_bytes)
	if not data:
		# EOF — child closed pty. Surface as OSError so the caller
		# breaks out of the loop instead of spin-reading 0 bytes.
		raise OSError('pty eof')
	return data


async def _run_with_timeout(
	proc: asyncio.subprocess.Process,
	timeout: float,
) -> tuple[bytes, bytes] | None:
	"""Wait for `proc` with a timeout; on timeout, terminate (then kill) it.

	asyncio.wait_for cancels the await but does NOT kill the subprocess —
	the child keeps running in the background, holding FDs and pages. Be
	explicit: TERM, give it 2s to exit cleanly, then KILL if still alive,
	then await communicate() so the loop reaps the zombie.

	Returns (stdout, stderr) on success, None on timeout / failure.
	"""
	try:
		return await asyncio.wait_for(proc.communicate(), timeout=timeout)
	except asyncio.TimeoutError:
		try:
			proc.terminate()
		except ProcessLookupError:
			return None
		try:
			return await asyncio.wait_for(proc.communicate(), timeout=2)
		except asyncio.TimeoutError:
			try:
				proc.kill()
			except ProcessLookupError:
				return None
			try:
				await proc.communicate()
			except Exception:
				pass
			return None


async def check_claude_authed() -> bool:
	"""Shell out to `claude auth status`; return True iff loggedIn.

	Claude prints JSON like `{"loggedIn": true, ...}` on stdout. Substring
	match is robust to minor key-order / whitespace formatting changes.
	"""
	try:
		proc = await asyncio.create_subprocess_exec(
			CLAUDE_BIN,
			'auth',
			'status',
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.STDOUT,
			env={**os.environ, 'HOME': '/home/bux'},
		)
	except Exception:
		return False
	res = await _run_with_timeout(proc, 15)
	if res is None:
		return False
	out, _ = res
	text = (out or b'').decode(errors='replace').lower()
	return '"loggedin": true' in text or '"loggedin":true' in text


async def check_codex_authed() -> bool:
	"""Shell out to `codex login status`; return True iff logged in.

	Codex prints human-readable status (e.g. "Logged in using ChatGPT")
	on success and "not logged in" / "command not found" / similar on
	failure. Matching plain-text substrings keeps us forward-compatible
	with minor copy changes; the false case (CLI not installed, etc.)
	just keeps codex_authed=false until the user installs / signs in.
	"""
	try:
		proc = await asyncio.create_subprocess_exec(
			CODEX_BIN,
			'login',
			'status',
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.STDOUT,
			env={**os.environ, 'HOME': '/home/bux'},
		)
	except Exception:
		return False
	res = await _run_with_timeout(proc, 15)
	if res is None:
		return False
	out, _ = res
	text = (out or b'').decode(errors='replace').lower()
	# Belt-and-braces: rc==0 alone isn't reliable across codex versions
	# (some print "not logged in" but still exit 0), so require the
	# explicit text marker. The "not" guard keeps us from false-positive
	# matching "you are not logged in".
	if 'not logged in' in text or 'command not found' in text:
		return False
	return 'logged in' in text


class ShellSession:
	"""A bash PTY whose bytes are streamed over the agent's WS channel.

	Life-cycle:
	  - opened by `shell_attach {session_id}` command
	  - stdout bytes sent as {type: "shell_chunk", session_id, data: <b64>}
	  - stdin received as {cmd: "shell_input", session_id, data: <b64>}
	  - resized via {cmd: "shell_resize", session_id, rows, cols}
	  - closed on process exit OR {cmd: "shell_close", session_id}
	"""

	def __init__(self, session_id: str, on_event) -> None:
		self.session_id = session_id
		self._on_event = on_event
		self._pid: int | None = None
		self._fd: int | None = None
		self._read_task: asyncio.Task | None = None

	def start(
		self,
		rows: int = 40,
		cols: int = 120,
		launch: str = 'claude',
		dsp_enabled: bool = True,
		window_id: str = 'bux-w1',
	) -> None:
		"""Attach the WS to a tmux window (creating the window on first attach).

		Why tmux: the WS comes and goes (page nav, mobile backgrounding,
		flaky network). Without tmux, every reconnect forks a fresh pty
		and the user loses scrollback + in-flight commands. With tmux,
		the pty + shell live in the tmux server independently of the
		WS; reattach reads the existing screen state, no reset.

		`window_id` is the tmux session name. The cloud picks it (default
		`bux-w1`); we sanitize defensively. First attach to a window
		creates it via `tmux new-session -A -d` and seeds the launch
		command (claude / codex / bash). Subsequent attaches just open another
		client onto the same tmux session.

		`launch` and `dsp_enabled` are only honored when we're CREATING
		the window. If the window already exists, we attach to whatever
		it's running — the user picked their flow on first launch and
		we don't want to clobber it on reconnect.
		"""
		import fcntl

		# Defensive: window_id must be safe to pass to tmux as -t. Allow
		# only [A-Za-z0-9_-]; fall back to bux-w1.
		import re
		import struct

		if not re.match(r'^[A-Za-z0-9_-]{1,64}$', window_id):
			window_id = 'bux-w1'

		# Ensure the tmux server + window exist before forking the pty.
		# Running synchronously on the asyncio event loop is fine — tmux
		# new-session -d returns in milliseconds and avoids a race where
		# our pty-side `attach` runs before the session is created.
		self._ensure_tmux_window(window_id, launch=launch, dsp_enabled=dsp_enabled)

		pid, fd = pty.fork()
		if pid == 0:
			os.environ['HOME'] = '/home/bux'
			os.environ['USER'] = 'bux'
			os.environ['SHELL'] = '/bin/bash'
			os.environ['TERM'] = 'xterm-256color'
			os.environ['LANG'] = 'C.UTF-8'
			try:
				os.chdir('/home/bux')
			except Exception:
				pass
			try:
				os.setgid(1001)
				os.setuid(1001)
			except Exception:
				pass
			try:
				# Attach to (or create) the tmux session. `-A` creates if
				# missing. `-x/-y` seed the size for first-create only;
				# reattach uses our subsequent TIOCSWINSZ + onResize.
				#
				# Without `aggressive-resize on`, tmux clamps to the
				# smallest connected client's size — bad if user has a
				# phone and a laptop both attached. With it on, tmux
				# uses the most recently active client's size.
				os.execvp(
					'/usr/bin/tmux',
					[
						'tmux',
						'new-session',
						'-A',
						'-s',
						window_id,
						'-x',
						str(cols),
						'-y',
						str(rows),
					],
				)
			except Exception as e:
				os.write(2, f'exec-failed: {e}\n'.encode())
				os._exit(127)
		self._pid = pid
		self._fd = fd
		winsz = struct.pack('HHHH', rows, cols, 0, 0)
		try:
			fcntl.ioctl(fd, termios.TIOCSWINSZ, winsz)
		except Exception:
			pass
		flags = fcntl.fcntl(fd, fcntl.F_GETFL)
		fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
		LOG.info(
			'shell %s pid=%s fd=%s %dx%d window=%s',
			self.session_id, pid, fd, rows, cols, window_id,
		)
		self._read_task = asyncio.create_task(self._read_loop())

	@staticmethod
	def _ensure_tmux_window(
		window_id: str, *, launch: str, dsp_enabled: bool
	) -> None:
		"""Idempotently create the tmux window if it doesn't exist.

		We do this from the parent process (as bux, via sudo) BEFORE
		forking the pty — the pty path then attaches into a known-good
		session. Splitting create-vs-attach this way avoids "session
		not found" races and lets us seed `launch` only on first create.
		"""
		import shlex
		import subprocess

		def _run(args: list[str]) -> int:
			# box-agent already runs as bux (systemd User=bux), so direct
			# /usr/bin/tmux invocations share the same default socket as
			# the pty child. No sudo gymnastics needed.
			try:
				return subprocess.run(
					['/usr/bin/tmux', *args],
					stdin=subprocess.DEVNULL,
					stdout=subprocess.DEVNULL,
					stderr=subprocess.DEVNULL,
					timeout=5,
				).returncode
			except Exception:
				return 1

		# `has-session` exits 0 if the window exists. Cheap probe.
		exists = _run(['has-session', '-t', window_id]) == 0
		if exists:
			return

		# Build the launch command — routed through tmux's first-window
		# command so the shell survives WS reconnects.
		if launch == 'claude':
			claude_cmd = (
				'claude --dangerously-skip-permissions' if dsp_enabled else 'claude'
			)
			# `; exec bash -l` so when claude quits the user lands in a
			# bash prompt instead of the tmux session ending.
			cmd_str = f'{claude_cmd}; exec bash -l'
		elif launch == 'codex':
			# Same tmux lifecycle as Claude: Codex owns the first command,
			# and quitting drops the user to a normal login shell.
			cmd_str = f'{shlex.quote(CODEX_BIN)}; exec bash -l'
		else:
			cmd_str = 'exec bash -l'

		# `-d` creates detached so we don't accidentally spin up a tmux
		# client in this process. The pty fork below will attach.
		_run([
			'new-session',
			'-d',
			'-s',
			window_id,
			'-x',
			'200',
			'-y',
			'50',
			'/bin/bash',
			'-lc',
			cmd_str,
		])
		# Last-attached-wins resize semantics — fixes phone+laptop
		# clobbering each other's column count.
		_run(['set-window-option', '-t', window_id, 'aggressive-resize', 'on'])

	async def _read_loop(self) -> None:
		import base64

		assert self._fd is not None and self._pid is not None
		loop = asyncio.get_event_loop()
		try:
			while True:
				ready = await loop.run_in_executor(
					None, lambda: select.select([self._fd], [], [], 1.0)[0]
				)
				if ready:
					try:
						chunk = os.read(self._fd, 4096)
					except (BlockingIOError, OSError):
						chunk = b''
					if not chunk:
						break
					await self._on_event(
						{
							'type': 'shell_chunk',
							'session_id': self.session_id,
							'data': base64.b64encode(chunk).decode(),
						}
					)
				try:
					done, _ = os.waitpid(self._pid, os.WNOHANG)
				except ChildProcessError:
					done = self._pid
				if done == self._pid:
					break
		except Exception:
			LOG.exception('shell read_loop crashed')
		finally:
			await self._on_event({'type': 'shell_closed', 'session_id': self.session_id})
			self._cleanup()

	async def write(self, data: bytes) -> None:
		"""Chunk writes to the pty.

		Pasting a 10kB+ blob into xterm.js used to drop characters because
		the pty's internal buffer (~4KB on Linux) returned a short write
		and we threw away the rest. Loop on os.write, retry EAGAIN with a
		short asyncio.sleep so the kernel has time to drain the line
		discipline. Async sleep — not time.sleep — so a sustained paste
		doesn't stall the event loop and starve heartbeats / other shells.
		"""
		if self._fd is None:
			return
		view = memoryview(data)
		while view:
			try:
				n = os.write(self._fd, bytes(view[:4096]))
			except BlockingIOError:
				# Buffer full. Yield to the loop and retry — the read
				# loop is draining concurrently. 5ms keeps a 100k paste
				# under 250ms total in the worst case.
				await asyncio.sleep(0.005)
				continue
			except Exception:
				LOG.exception('shell write failed')
				return
			if n <= 0:
				return
			view = view[n:]

	def resize(self, rows: int, cols: int) -> None:
		import fcntl
		import struct

		if self._fd is None:
			return
		winsz = struct.pack('HHHH', rows, cols, 0, 0)
		try:
			fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsz)
		except Exception:
			pass

	def close(self) -> None:
		self._cleanup()

	def _cleanup(self) -> None:
		if self._fd is not None:
			try:
				os.close(self._fd)
			except OSError:
				pass
			self._fd = None
		if self._pid is not None:
			try:
				os.kill(self._pid, signal.SIGHUP)
			except ProcessLookupError:
				pass
			try:
				os.waitpid(self._pid, os.WNOHANG)
			except ChildProcessError:
				pass
			self._pid = None


class Agent:
	def __init__(self, cloud_url: str, box_token: str, box_id: str) -> None:
		self.ws_url = cloud_url.rstrip('/') + '/api/v3/boxes/ws'
		self.box_token = box_token
		self.box_id = box_id
		# `websockets` dropped the public re-export of WebSocketClientProtocol —
		# we only use it as the return type of websockets.connect(), so an
		# untyped annotation is fine.
		self.ws = None  # type: ignore[assignment]
		self._stop = asyncio.Event()
		self._authed = False  # last-known claude auth state
		self._auth_wakeup = asyncio.Event()  # poke to force immediate recheck
		self._shells: dict[str, ShellSession] = {}
		# Claude-login state machine (driven by `claude_login_*` cmds from cloud).
		# A single in-flight login attempt at a time; new claude_login_start kills
		# any prior one. See _claude_login_start. The monotonic `_attempt`
		# counter disambiguates pty file descriptors across retries: kernels
		# recycle fd numbers immediately after close, so an auto-Enter timer
		# scheduled for attempt N must check the counter (not just the fd
		# value) before writing to avoid bleeding into attempt N+1's pty.
		self._claude_login_pid: int | None = None
		self._claude_login_fd: int | None = None
		self._claude_login_task: asyncio.Task | None = None
		self._claude_login_attempt: int = 0
		# Codex auth state — symmetric mirror of the claude side. Codex's
		# device-auth flow (`codex login --device-auth`) prints both a URL
		# and a one-time code on stdout, then waits for browser-side
		# authorization and exits 0. No paste-the-code-back step (unlike
		# claude), so the cmd dispatch only handles `codex_login_start` and
		# `codex_login_cancel`.
		self._codex_authed = False
		self._codex_auth_wakeup = asyncio.Event()
		self._codex_login_pid: int | None = None
		self._codex_login_fd: int | None = None
		self._codex_login_task: asyncio.Task | None = None
		self._codex_login_attempt: int = 0
		# Strong refs for fire-and-forget tasks (run_task dispatches). The
		# event loop only weak-refs tasks from asyncio.create_task; without
		# this set the GC can collect them mid-run and drop the output.
		self._bg_tasks: set[asyncio.Task] = set()
		# DSP = "dangerously skip permissions". When True, new claude shell
		# sessions launch with --dangerously-skip-permissions. Source of
		# truth is the cloud DB; cloud sends update_dsp on connect (`hello`
		# response) and whenever the user toggles it in the /bux UI.
		# Default True matches the cloud-side policy (BoxModel default is
		# also True). The cloud always sends an authoritative update_dsp on
		# every reconnect, so local default only matters in the millisecond
		# window before that message — keeping it aligned with policy means
		# any race window favors the user's intent rather than fighting it.
		self._dsp_enabled = True
		# Last reported browser session id (from /home/bux/.claude/browser.env,
		# written by browser-keeper). We poll the file and notify cloud on
		# change so BoxView.live_browser_url stays accurate across rotations.
		self._last_browser_id: str | None = None

	async def run(self) -> None:
		backoff = 1
		while not self._stop.is_set():
			try:
				await self._connect_once()
				backoff = 1
			except Exception as e:
				LOG.warning('ws connect failed: %s; retry in %ds', e, backoff)
				await asyncio.sleep(backoff)
				backoff = min(backoff * 2, 60)

	async def _connect_once(self) -> None:
		# Warm claude's filesystem cache + plugin hydration BEFORE we tell
		# cloud we're alive. Cloud flips status PROVISIONING → AWAITING_OAUTH
		# the moment we send `hello`, and that's the signal for the frontend
		# to open the terminal iframe — at which point the user expects
		# claude to launch instantly. Without this prewarm, the first
		# `claude` invocation on a freshly-booted box has to fault all the
		# bake-time-cached pages back in from disk, which feels like 20-30s
		# of dead air.
		#
		# Idempotent across reconnects: only the first connect actually
		# spawns claude; subsequent reconnects (WS flap, etc.) skip.
		if not getattr(self, '_prewarmed', False):
			LOG.info('prewarming claude before announcing AWAITING_OAUTH...')
			outcome = 'failed'
			try:
				proc = await asyncio.create_subprocess_exec(
					CLAUDE_BIN,
					'--version',
					stdout=asyncio.subprocess.DEVNULL,
					stderr=asyncio.subprocess.DEVNULL,
					env={**os.environ, 'HOME': '/home/bux'},
				)
				# _run_with_timeout terminates / kills the child on timeout
				# so we never leave an orphaned `claude` subprocess holding
				# FDs and memory if the prewarm hangs. Returns None on
				# timeout so we can log it distinctly — silently treating
				# a 30s hang as success would hide a real prod regression.
				res = await _run_with_timeout(proc, 30)
				if res is None:
					outcome = 'timed-out (subprocess killed)'
				elif proc.returncode == 0:
					outcome = 'ok'
				else:
					outcome = f'non-zero exit ({proc.returncode})'
			except Exception:
				LOG.exception('claude prewarm errored')
			self._prewarmed = True
			# Always log the outcome so operators can tell from agent logs
			# whether the box has hot claude pages cached. The hello → status
			# flip below proceeds either way; a slow first claude is bad UX
			# but not a hard failure.
			LOG.info('claude prewarm: %s', outcome)

		headers = [('Authorization', f'Bearer {self.box_token}')]
		async with websockets.connect(self.ws_url, additional_headers=headers) as ws:
			self.ws = ws
			LOG.info('connected to %s', self.ws_url)
			await self._send({
				'type': 'hello',
				'box_id': self.box_id,
				'agent_version': '0.4.0',
				'agent_sha': _get_agent_sha(),
				'agent_branch': _get_agent_branch(),
				# Backfill the cloud's tg_bot_username column for boxes
				# provisioned before we started persisting the handle in
				# install_telegram(). Cloud only writes the row if it's
				# currently empty — destroy / re-up are still the
				# authoritative clear paths. None when bux-tg isn't set
				# up yet (no /etc/bux/tg.env on this box).
				'tg_bot_username': _read_tg_bot_username(),
			})

			hb_task = asyncio.create_task(self._heartbeat_loop())
			auth_task = asyncio.create_task(self._auth_poll_loop())
			codex_auth_task = asyncio.create_task(self._codex_auth_poll_loop())
			browser_task = asyncio.create_task(self._browser_id_poll_loop())
			try:
				async for raw in ws:
					await self._handle(raw)
			finally:
				browser_task.cancel()
				hb_task.cancel()
				auth_task.cancel()
				codex_auth_task.cancel()
				self.ws = None

	async def _heartbeat_loop(self) -> None:
		try:
			while True:
				await asyncio.sleep(HEARTBEAT_INTERVAL)
				if self.ws is None:
					return
				await self._send({'type': 'heartbeat'})
		except asyncio.CancelledError:
			return

	async def _auth_poll_loop(self) -> None:
		"""Poll `claude auth status`; notify cloud when state flips.

		Fast polling (1s) for the first 60s after connect — user is likely
		mid-`bux up` flow. After that, drop to 15s (claude logout is rare,
		don't waste CPU).

		`_auth_wakeup` event lets other handlers force an immediate recheck
		without waiting for the next tick (currently no callers; kept for
		future use).
		"""
		start = asyncio.get_event_loop().time()
		try:
			while True:
				authed = await check_claude_authed()
				if authed != self._authed:
					self._authed = authed
					if authed:
						await self._send({'type': 'claude_authed'})
						LOG.info('claude is authed — notified cloud')
					else:
						await self._send({'type': 'claude_auth_failed'})
				# Fast for the first minute, slow after.
				elapsed = asyncio.get_event_loop().time() - start
				interval = 1.0 if elapsed < 60 else 15.0
				try:
					await asyncio.wait_for(self._auth_wakeup.wait(), timeout=interval)
					self._auth_wakeup.clear()
				except asyncio.TimeoutError:
					pass
		except asyncio.CancelledError:
			return
		except Exception:
			LOG.exception('auth_poll_loop crashed')

	async def _codex_auth_poll_loop(self) -> None:
		"""Poll `codex login status`; notify cloud on flips. Mirror of
		_auth_poll_loop but for codex. Same 1s/15s cadence so a successful
		device-auth flow surfaces to the FE within the first second."""
		start = asyncio.get_event_loop().time()
		try:
			while True:
				authed = await check_codex_authed()
				if authed != self._codex_authed:
					self._codex_authed = authed
					if authed:
						await self._send({'type': 'codex_authed'})
						LOG.info('codex is authed — notified cloud')
					else:
						await self._send({'type': 'codex_auth_failed'})
				elapsed = asyncio.get_event_loop().time() - start
				interval = 1.0 if elapsed < 60 else 15.0
				try:
					await asyncio.wait_for(
						self._codex_auth_wakeup.wait(), timeout=interval
					)
					self._codex_auth_wakeup.clear()
				except asyncio.TimeoutError:
					pass
		except asyncio.CancelledError:
			return
		except Exception:
			LOG.exception('codex_auth_poll_loop crashed')

	async def _browser_id_poll_loop(self) -> None:
		"""Watch BROWSER_ENV_PATH for BU_BROWSER_ID changes.

		The keeper rewrites the file on each rotation (~every 209 min). We
		poll its mtime cheaply and re-parse on change, sending one
		`browser_update` over WS whenever the id moves. Initial value is
		also reported so the cloud row is correct on (re)connect.

		Polling beats inotify here: the `aiofiles`/`watchdog` deps aren't
		on the box's tiny venv, and a 30 s mtime check is essentially free.
		"""
		last_mtime = -1.0
		# Send the current value once on connect, regardless of mtime — the
		# cloud row may have drifted while we were disconnected.
		await self._maybe_report_browser_id(force=True)
		try:
			while True:
				await asyncio.sleep(30)
				try:
					mtime = os.path.getmtime(BROWSER_ENV_PATH)
				except FileNotFoundError:
					mtime = -1.0
				if mtime != last_mtime:
					last_mtime = mtime
					await self._maybe_report_browser_id()
		except asyncio.CancelledError:
			return
		except Exception:
			LOG.exception('browser_id_poll_loop crashed')

	async def _maybe_report_browser_id(self, force: bool = False) -> None:
		bid: str | None = None
		try:
			with open(BROWSER_ENV_PATH) as f:
				for line in f:
					if line.startswith('BU_BROWSER_ID='):
						bid = line.split('=', 1)[1].strip() or None
						break
		except FileNotFoundError:
			bid = None
		except Exception:
			LOG.exception('reading %s failed', BROWSER_ENV_PATH)
			return
		if not force and bid == self._last_browser_id:
			return
		self._last_browser_id = bid
		LOG.info('browser_id → %s', bid)
		await self._send({'type': 'browser_update', 'browser_id': bid})

	async def _send(self, msg: dict) -> None:
		if self.ws is None:
			return
		try:
			await self.ws.send(json.dumps(msg))
		except Exception:
			LOG.exception('send failed')

	async def _handle(self, raw: str | bytes) -> None:
		try:
			msg = json.loads(raw)
		except Exception:
			LOG.warning('non-json: %r', raw[:200])
			return
		cmd = msg.get('cmd')
		LOG.info('recv cmd=%s', cmd)

		if cmd == 'run_task':
			t = asyncio.create_task(self._run_task(msg.get('task_id'), msg.get('prompt', '')))
			self._bg_tasks.add(t)
			t.add_done_callback(self._bg_tasks.discard)
		elif cmd == 'shell_attach':
			sid = msg.get('session_id')
			if not sid:
				return
			rows = int(msg.get('rows') or 40)
			cols = int(msg.get('cols') or 120)
			launch = msg.get('launch') or 'claude'
			# Default window so existing single-terminal callers keep
			# working without code changes.
			window_id = msg.get('window_id') or 'bux-w1'
			if sid in self._shells:
				self._shells[sid].close()
			sh = ShellSession(sid, on_event=self._send)
			self._shells[sid] = sh
			sh.start(
				rows=rows,
				cols=cols,
				launch=launch,
				dsp_enabled=self._dsp_enabled,
				window_id=window_id,
			)
		elif cmd in ('windows_list', 'windows_create', 'windows_delete', 'windows_rename'):
			# Tmux window CRUD. Reply on the WS with `windows_<cmd>_result`.
			# Keep these synchronous — tmux returns in <50ms and async
			# subprocess machinery is overkill.
			await self._handle_windows_cmd(msg)
		elif cmd == 'update_dsp':
			# Cloud → agent. We mirror the flag in memory; next shell_attach
			# with launch=claude picks up the new value. Existing sessions
			# are unaffected (they keep whatever flag they spawned with).
			new = bool(msg.get('enabled', False))
			if new != self._dsp_enabled:
				LOG.info('dsp_enabled %s → %s', self._dsp_enabled, new)
				self._dsp_enabled = new
		elif cmd == 'update_default_agent':
			# Cloud owns the persisted default. The launch choice is carried
			# on each shell_attach, so the box-agent only ACKs this command to
			# avoid warning noise on older/no-op setting pushes.
			await self._send({'type': 'ack', 'cmd': cmd, 'ok': True})
		elif cmd == 'shell_input':
			import base64 as _b64

			sid = msg.get('session_id')
			sh = self._shells.get(sid)
			if sh is not None:
				try:
					await sh.write(_b64.b64decode(msg.get('data', '')))
				except Exception:
					LOG.exception('shell_input decode failed')
		elif cmd == 'shell_resize':
			sid = msg.get('session_id')
			sh = self._shells.get(sid)
			if sh is not None:
				sh.resize(int(msg.get('rows') or 40), int(msg.get('cols') or 120))
		elif cmd == 'shell_close':
			sid = msg.get('session_id')
			sh = self._shells.pop(sid, None)
			if sh is not None:
				sh.close()
		elif cmd == 'tg_install':
			# `owner_tg_user_id` is optional. When cloud knows who the
			# legitimate owner is on Telegram (BuxFather flow has it from
			# the manager-bot creator update), it propagates the user_id
			# here so the bot's bind gate can auto-bind the owner's first
			# DM without requiring `/start <setup_token>`. Paste / QR
			# flows leave it None — those installs keep the strict-token
			# bind gate as the only accept path.
			raw_owner = msg.get('owner_tg_user_id')
			try:
				owner_tg_user_id: int | None = int(raw_owner) if raw_owner else None
			except (TypeError, ValueError):
				owner_tg_user_id = None
			await self._tg_install(
				msg.get('bot_token', ''),
				msg.get('setup_token', ''),
				msg.get('bot_username', ''),
				owner_tg_user_id=owner_tg_user_id,
			)
		elif cmd == 'update':
			# Pull latest agent code from the OSS repo and restart services.
			# Defaults to the branch the box was originally cloned from
			# (whatever's checked out in /opt/bux/repo); cmd can pass
			# `branch` to switch tracks, e.g. `stable` → `main`.
			await self._update(
				branch=msg.get('branch') or '',
				request_id=msg.get('request_id') or '',
			)
		elif cmd == 'claude_login_start':
			# Drive `claude /login` from a pty so the cloud can extract
			# the OAuth URL and pump the callback code back without
			# making the user copy/paste in a phone-hostile terminal.
			# See _claude_login_start for the state machine.
			await self._claude_login_start()
		elif cmd == 'claude_login_code':
			await self._claude_login_code(code=msg.get('code', ''))
		elif cmd == 'claude_login_cancel':
			await self._claude_login_cancel()
		elif cmd == 'codex_login_start':
			# Codex device-auth flow. Unlike claude, no code paste step
			# — we extract URL + one-time-code from stdout, surface them
			# both to cloud, and the codex CLI auto-completes when the
			# user authorizes in the browser. See _codex_login_start.
			await self._codex_login_start()
		elif cmd == 'codex_login_cancel':
			await self._codex_login_cancel()
		elif cmd == 'ping':
			await self._send({'type': 'pong'})
		else:
			LOG.warning('unknown cmd=%s', cmd)
			await self._send({'type': 'ack', 'cmd': cmd, 'ok': False, 'error': 'unknown-cmd'})

	def _session_args(self) -> list[str]:
		"""Return the claude CLI args that pin/reuse this box's session.

		First call (no session on disk): `--session-id <new-uuid>` creates it.
		Subsequent calls: `--resume <uuid>` reuses the same conversation.

		The session file is shared with the root-running bux-tg service, so
		both sides use O_NOFOLLOW to be symlink-safe (see telegram_bot.py).
		"""
		path = '/home/bux/.bux/session'
		try:
			fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
			try:
				with os.fdopen(fd, 'r') as f:
					sid = f.read().strip()
			except Exception:
				os.close(fd)
				raise
			if len(sid) == 36 and sid.count('-') == 4:
				return ['--resume', sid]
		except FileNotFoundError:
			pass
		except OSError as e:
			LOG.warning('reading %s failed (%s); regenerating', path, e)

		sid = str(uuid.uuid4())
		os.makedirs(os.path.dirname(path), exist_ok=True)
		try:
			fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
		except OSError as e:
			LOG.warning('creating %s failed (%s); session not persisted', path, e)
			return ['--session-id', sid]
		try:
			with os.fdopen(fd, 'w') as f:
				f.write(sid)
		except Exception:
			os.close(fd)
			raise
		LOG.info('created new bux claude session_id=%s', sid)
		return ['--session-id', sid]

	async def _run_task(self, task_id: str | None, prompt: str) -> None:
		"""Run `claude -p "<prompt>"` as bux user, stream stdout over WS.

		Sends events:
		  - {type: task_chunk, task_id, data: "..."}  — stdout chunks
		  - {type: task_done, task_id, rc: int}       — final exit code
		"""
		if not task_id or not prompt:
			await self._send({'type': 'task_done', 'task_id': task_id, 'rc': 2, 'error': 'invalid'})
			return
		LOG.info('run_task %s: %s', task_id, prompt[:120])
		# Agent runs as the `bux` user already (per systemd). Spawn claude
		# directly — no sudo needed. Forward BU envs for browser-harness.
		box_env = load_env()
		child_env = {
			**os.environ,
			'HOME': '/home/bux',
			'USER': 'bux',
			'PATH': '/usr/local/bin:/usr/bin:/bin:' + os.environ.get('PATH', ''),
		}
		if box_env.get('BROWSER_USE_API_KEY'):
			child_env['BROWSER_USE_API_KEY'] = box_env['BROWSER_USE_API_KEY']
		if box_env.get('BUX_PROFILE_ID'):
			child_env['BU_PROFILE_ID'] = box_env['BUX_PROFILE_ID']
			child_env['BUX_PROFILE_ID'] = box_env['BUX_PROFILE_ID']

		session_args = self._session_args()

		# Serialize across processes (box-agent + bux-tg). claude takes an
		# exclusive lock on the session file while running; a second claude
		# against the same uuid would fail with "session in use." We flock a
		# dedicated file so both services queue behind each other cleanly.
		lock_path = '/home/bux/.bux/claude.lock'
		os.makedirs(os.path.dirname(lock_path), exist_ok=True)
		loop = asyncio.get_running_loop()
		# O_NOFOLLOW: refuse to open through a symlink; we run as the bux user
		# so the blast radius is small, but the TG bot (root) shares this file
		# and we want both sides symlink-safe.
		#
		# Mode 0664 so the bux group can write. Historical bug: TG bot (root)
		# created this file 0644 root:root, which locked box-agent (bux) out
		# with Permission denied. Both sides now create it group-writable;
		# TG also fchowns to bux (see telegram_bot._open_lockfile).
		lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o664)
		await loop.run_in_executor(None, fcntl.flock, lock_fd, fcntl.LOCK_EX)
		try:
			try:
				proc = await asyncio.create_subprocess_exec(
					'/usr/bin/claude',
					'-p',
					*session_args,
					'--output-format',
					'text',
					'--permission-mode',
					'bypassPermissions',
					prompt,
					stdout=asyncio.subprocess.PIPE,
					stderr=asyncio.subprocess.STDOUT,
					cwd='/home/bux',
					env=child_env,
				)
			except Exception:
				LOG.exception('run_task spawn failed')
				await self._send(
					{'type': 'task_done', 'task_id': task_id, 'rc': 127, 'error': 'spawn-failed'}
				)
				return

			assert proc.stdout is not None
			try:
				while True:
					chunk = await proc.stdout.read(4096)
					if not chunk:
						break
					await self._send(
						{
							'type': 'task_chunk',
							'task_id': task_id,
							'data': chunk.decode(errors='replace'),
						}
					)
				rc = await proc.wait()
				await self._send({'type': 'task_done', 'task_id': task_id, 'rc': rc})
				LOG.info('run_task %s done rc=%s', task_id, rc)
			except Exception:
				LOG.exception('run_task stream failed')
				try:
					proc.kill()
				except ProcessLookupError:
					pass
				await self._send(
					{'type': 'task_done', 'task_id': task_id, 'rc': -1, 'error': 'stream-failed'}
				)
		finally:
			fcntl.flock(lock_fd, fcntl.LOCK_UN)
			os.close(lock_fd)

	# ------------------------------------------------------------------
	# Tmux window CRUD. Each window is a tmux session named `bux-w<n>`
	# carrying an optional human label (`@bux-label` user option).
	# Cloud → agent: `windows_<verb>` cmd with a request_id. Agent →
	# cloud: `windows_<verb>_result` with the same request_id so the
	# caller can correlate replies. Synchronous tmux calls — they're
	# all <50ms.
	# ------------------------------------------------------------------

	async def _handle_windows_cmd(self, msg: dict) -> None:
		import re
		import subprocess

		cmd = msg.get('cmd', '')
		req_id = msg.get('request_id') or ''

		def _tmux(args: list[str]) -> tuple[int, str]:
			try:
				r = subprocess.run(
					['/usr/bin/tmux', *args],
					stdin=subprocess.DEVNULL,
					capture_output=True,
					text=True,
					timeout=5,
				)
				return r.returncode, (r.stdout or '').strip()
			except Exception as e:
				return 1, str(e)

		def _safe_id(s: str) -> str | None:
			# Reject anything that could be a tmux flag or shell metachar.
			# Window ids are auto-generated bux-w<n> so the spec is tight.
			if not isinstance(s, str):
				return None
			if not re.match(r'^bux-w[0-9]{1,4}$', s):
				return None
			return s

		async def _reply(payload: dict) -> None:
			await self._send({
				'type': f'{cmd}_result',
				'request_id': req_id,
				**payload,
			})

		if cmd == 'windows_list':
			# Format: name|created|attached|label per session, only those
			# starting with `bux-w`. We get the label from a per-session
			# user option (@bux-label). `#{?...}` is tmux's ternary; we
			# emit empty string when unset rather than the literal "@…".
			rc, out = _tmux([
				'list-sessions',
				'-F',
				'#{session_name}|#{session_created}|#{?session_attached,1,0}|#{@bux-label}',
			])
			windows: list[dict] = []
			if rc == 0:
				for line in out.splitlines():
					parts = line.split('|', 3)
					if len(parts) < 3:
						continue
					name = parts[0]
					if not name.startswith('bux-w'):
						continue
					try:
						created = int(parts[1])
					except (ValueError, IndexError):
						created = 0
					attached = parts[2] == '1'
					label = parts[3] if len(parts) > 3 else ''
					windows.append({
						'id': name,
						'label': label,
						'created_at': created,
						'attached': attached,
					})
			# Stable order: oldest first matches the natural counter.
			windows.sort(key=lambda w: w['created_at'])
			await _reply({'ok': rc == 0, 'windows': windows})
			return

		if cmd == 'windows_create':
			label = (msg.get('label') or '')[:64]
			# Pick the next free bux-w<n>. Race-free enough for our use:
			# windows_create is human-driven and serialized through the
			# WS, so two concurrent creates from the same user are
			# impossible.
			rc, out = _tmux(['list-sessions', '-F', '#{session_name}'])
			used: set[int] = set()
			if rc == 0:
				for line in out.splitlines():
					m = re.match(r'^bux-w([0-9]+)$', line)
					if m:
						used.add(int(m.group(1)))
			n = 1
			while n in used:
				n += 1
			window_id = f'bux-w{n}'
			# Spawn detached running bash. We don't auto-launch claude
			# here — that's a "first window" thing handled by
			# ShellSession. Subsequent windows are bash by default; users
			# can `claude` themselves.
			rc, _ = _tmux([
				'new-session',
				'-d',
				'-s',
				window_id,
				'-x',
				'200',
				'-y',
				'50',
				'/bin/bash',
				'-l',
			])
			if rc != 0:
				await _reply({'ok': False, 'error': 'tmux-create-failed'})
				return
			_tmux(['set-window-option', '-t', window_id, 'aggressive-resize', 'on'])
			if label:
				_tmux(['set-option', '-t', window_id, '@bux-label', label])
			await _reply({
				'ok': True,
				'window': {
					'id': window_id,
					'label': label,
					'attached': False,
				},
			})
			return

		if cmd == 'windows_delete':
			window_id = _safe_id(msg.get('window_id', ''))
			if window_id is None:
				await _reply({'ok': False, 'error': 'bad-window-id'})
				return
			rc, _ = _tmux(['kill-session', '-t', window_id])
			await _reply({'ok': rc == 0})
			return

		if cmd == 'windows_rename':
			window_id = _safe_id(msg.get('window_id', ''))
			if window_id is None:
				await _reply({'ok': False, 'error': 'bad-window-id'})
				return
			label = (msg.get('label') or '')[:64]
			rc, _ = _tmux(['set-option', '-t', window_id, '@bux-label', label])
			if rc != 0:
				await _reply({'ok': False, 'error': 'tmux-rename-failed'})
				return
			# Read back the full window state — created_at + attached —
			# so the cloud's PATCH route can hand the FE a complete view
			# instead of fabricating attached=False / created_at=0.
			rc2, out = _tmux([
				'display-message',
				'-p',
				'-t',
				window_id,
				'#{session_created}|#{?session_attached,1,0}',
			])
			created = 0
			attached = False
			if rc2 == 0:
				parts = out.split('|', 1)
				try:
					created = int(parts[0])
				except (ValueError, IndexError):
					created = 0
				attached = len(parts) > 1 and parts[1] == '1'
			await _reply({
				'ok': True,
				'window': {
					'id': window_id,
					'label': label,
					'created_at': created,
					'attached': attached,
				},
			})
			return

	async def _update(self, *, branch: str = '', request_id: str = '') -> None:
		"""Pull latest agent code from the OSS repo and restart services.

		Steps:
		  1. `git fetch` to grab the latest refs.
		  2. If `branch` was passed, switch to it. Otherwise stay on the
		     currently-checked-out branch.
		  3. `git reset --hard origin/<branch>` to advance.
		  4. `bash bootstrap.sh` to re-apply systemd units / polkit / cron
		     in case they changed (idempotent).
		  5. systemd restarts box-agent itself, killing this process. Reply
		     before that happens so the cloud sees `update_result` ok.

		If anything fails, we DON'T roll back yet — that's a follow-up
		feature. For now, the next user-triggered Update tries again.
		"""
		import subprocess

		old_sha = _get_agent_sha()

		def _run(args: list[str], cwd: str = str(REPO_DIR)) -> tuple[int, str]:
			try:
				r = subprocess.run(
					args,
					cwd=cwd,
					capture_output=True,
					text=True,
					timeout=60,
				)
				return r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()
			except Exception as e:
				return 1, str(e)

		# Determine target branch: explicit > current.
		target_branch = branch or _get_agent_branch()
		if target_branch == 'unknown':
			target_branch = 'main'

		# install.sh clones with --branch main, which creates a single-
		# branch remote (refs/heads/main:refs/remotes/origin/main). A
		# bare `git fetch origin` then only pulls main, and `reset --hard
		# origin/<other>` fails with "unknown revision". Widen the remote
		# refspec to all branches the first time we update — idempotent
		# (--replace-all overwrites any existing single-branch refspec).
		rc, out = _run([
			'git', 'config', '--replace-all',
			'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*',
		])
		if rc != 0:
			LOG.warning('update: widening fetch refspec failed: %s', out)
			# Non-fatal — the explicit fetch below still works on
			# single-branch clones for the requested ref.

		# Fetch the target branch explicitly. The `:refs/remotes/...`
		# form works whether or not the refspec was successfully widened,
		# and survives single-branch clones from before this fix shipped.
		rc, out = _run([
			'git', 'fetch', '--prune', 'origin',
			f'+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}',
		])
		if rc != 0:
			LOG.warning('update: git fetch failed: %s', out)
			await self._send({
				'type': 'update_result', 'request_id': request_id,
				'ok': False,
				'error': f'fetch {target_branch}: {out[:200]}',
			})
			return

		# checkout -B (not reset --hard) so HEAD's symbolic-ref actually
		# points at the requested branch. `reset --hard origin/<x>`
		# moves whatever-branch-we're-on to the target commit but
		# doesn't switch branches — so /version on the box reports the
		# stale branch name even after a successful update. -B force-
		# creates/resets the local branch to track origin's, idempotent.
		rc, out = _run(['git', 'checkout', '-B', target_branch, '--track', f'origin/{target_branch}'])
		if rc != 0:
			LOG.warning('update: git checkout failed: %s', out)
			await self._send({
				'type': 'update_result', 'request_id': request_id,
				'ok': False,
				'error': f'checkout: {out[:200]}',
			})
			return

		new_sha = _get_agent_sha()
		LOG.info('update: %s → %s on %s', old_sha, new_sha, target_branch)

		# Re-apply systemd units / polkit / cron / pip deps. Idempotent.
		# Runs via sudo because bootstrap.sh writes /etc/systemd/* etc.
		# Sudoers entry is set up in bootstrap.sh itself for self-bootstrapping.
		rc, out = _run(
			['sudo', '/bin/bash', str(REPO_DIR / 'agent' / 'bootstrap.sh')],
			cwd='/',
		)
		if rc != 0:
			LOG.warning('update: bootstrap failed: %s', out)
			await self._send({
				'type': 'update_result', 'request_id': request_id,
				'ok': False,
				'old_sha': old_sha,
				'new_sha': new_sha,
				'error': f'bootstrap: {out[:200]}',
			})
			return

		# Reply BEFORE systemctl restarts us. bootstrap.sh ends with a
		# `systemctl restart box-agent`, which is what swaps in the new
		# code — by the time it runs, this coroutine is dead.
		await self._send({
			'type': 'update_result', 'request_id': request_id,
			'ok': True,
			'old_sha': old_sha,
			'new_sha': new_sha,
			'branch': target_branch,
		})
		# bootstrap.sh already restarted us at the end of its run.
		# When systemd kills us mid-coroutine, the WS just hangs up;
		# the new agent process picks up from a fresh hello.

	async def _tg_install(
		self,
		bot_token: str,
		setup_token: str,
		bot_username: str,
		*,
		owner_tg_user_id: int | None = None,
	) -> None:
		if not bot_token:
			await self._send(
				{'type': 'ack', 'cmd': 'tg_install', 'ok': False, 'error': 'empty-token'}
			)
			return
		lines = [f'TG_BOT_TOKEN={bot_token}']
		if setup_token:
			lines.append(f'TG_SETUP_TOKEN={setup_token}')
		if bot_username:
			lines.append(f'TG_BOT_USERNAME={bot_username}')
		if owner_tg_user_id:
			# `_box_owner()` in agent/telegram_bot.py reads TG_OWNER_ID as the
			# authoritative source of "who owns this box" — overriding the
			# state-file fallback. When set, it lets the bind gate auto-bind
			# the owner's first private DM without requiring /start <token>.
			# The convention (TG_OWNER_ID / TG_OWNER_USERNAME / TG_OWNER_NAME)
			# pre-dates this writer; we only need user_id for auth purposes
			# (Telegram stamps `from.id` server-side, can't be forged).
			lines.append(f'TG_OWNER_ID={int(owner_tg_user_id)}')
		TG_ENV.write_text('\n'.join(lines) + '\n', encoding='utf-8')
		# Mode 0o600, owner bux:bux (we run as bux). Both readers can still
		# get the token: the bux-telegram-bot.service runs as User=root
		# (root reads everything), and the tg-send helper runs as bux.
		#
		# We deliberately do NOT chown to root:bux here even though that's
		# what the systemd unit might prefer aesthetically — an unprivileged
		# process can't chown a file to uid 0, and the prior version that
		# attempted `os.chown(TG_ENV, 0, bux_gid)` raised PermissionError,
		# rolled the file back, and silently kept tg.env from existing at
		# all. That broke /start binding entirely (the bot service has
		# ConditionPathExists=/etc/bux/tg.env, so it never started). If we
		# ever want stricter perms (e.g. group-readable, owner=root) we'd
		# need a sudoers rule or a setuid helper — not worth the surface
		# area for a token that's already scoped to a single bound chat.
		try:
			os.chmod(TG_ENV, 0o600)
		except Exception as e:
			LOG.exception('chmod %s failed; aborting tg_install', TG_ENV)
			# Roll back the env file so a future retry doesn't see a stale
			# tg.env and skip rewriting. The systemd unit's
			# ConditionPathExists will then keep bux-telegram-bot.service
			# stopped until the user retries.
			try:
				TG_ENV.unlink()
			except Exception:
				LOG.exception('also failed to remove %s after perm error', TG_ENV)
			await self._send(
				{'type': 'ack', 'cmd': 'tg_install', 'ok': False, 'error': f'chmod-failed: {e}'}
			)
			return
		# Remove any stale allow list so the new setup_token actually gates pairing.
		for stale in (
			Path('/etc/bux/tg-allowed.txt'),
			Path('/etc/bux/tg-state.json'),
		):
			try:
				stale.unlink()
			except FileNotFoundError:
				pass
			except Exception:
				LOG.exception('could not remove %s', stale)
		# systemctl restart is a no-op on a unit that's never been started.
		# Explicit stop-then-start guarantees a fresh process with the new token.
		# We MUST `await proc.wait()` between them — `create_subprocess_exec`
		# only spawns the child, so without the wait `start` can race with
		# `stop` and we end up with two overlapping bux-tg processes fighting
		# over the same bot token (double getUpdates, double replies).
		try:
			stop_proc = await asyncio.create_subprocess_exec(
				'systemctl',
				'stop',
				'bux-tg.service',
				stdout=asyncio.subprocess.DEVNULL,
				stderr=asyncio.subprocess.DEVNULL,
			)
			await stop_proc.wait()
			start_proc = await asyncio.create_subprocess_exec(
				'systemctl',
				'start',
				'bux-tg.service',
				stdout=asyncio.subprocess.DEVNULL,
				stderr=asyncio.subprocess.DEVNULL,
			)
			rc = await start_proc.wait()
			if rc != 0:
				LOG.warning('systemctl start bux-tg exited rc=%s', rc)
			for unit in ('bux-miniapp.service', 'bux-miniapp-tunnel.service'):
				start_proc = await asyncio.create_subprocess_exec(
					'systemctl',
					'start',
					unit,
					stdout=asyncio.subprocess.DEVNULL,
					stderr=asyncio.subprocess.DEVNULL,
				)
				rc = await start_proc.wait()
				if rc != 0:
					LOG.warning('systemctl start %s exited rc=%s', unit, rc)
		except Exception:
			LOG.exception('start Telegram services failed')
		await self._send({'type': 'ack', 'cmd': 'tg_install', 'ok': True})


	# ------------------------------------------------------------------
	# Claude-Code OAuth login flow.
	#
	# `claude auth login` is interactive: claude prints an OAuth URL,
	# the user signs in, copies the callback URL/code, and pastes it
	# back. On a phone web terminal the copy/paste round trip via
	# xterm.js is brutal — so cloud's FE drives the same flow over
	# WebSocket, with this agent forking the pty and shuttling stdout
	# (URL) and stdin (pasted code) over `claude_login_*` events.
	#
	# Implementation tracks the working /claude login flow in the OSS
	# Telegram bot (telegram_bot.py:_cmd_claude_login → ShellSession):
	#   1. Fork pty, exec `/usr/bin/claude auth login`.
	#   2. Strip ANSI from each pty read, regex-match the OAuth URL.
	#      COLUMNS=1000 keeps claude from wrapping the ~700-char URL.
	#   3. On `claude_login_code` cmd, write `code\r\n` to the pty,
	#      then auto-send a blank Enter ~2s later to clear the
	#      "Press Enter to continue" confirmation prompt claude shows
	#      after a successful paste.
	#
	# Earlier attempts on this agent drove the *interactive* TUI by
	# sending `/login` as a slash command. That worked but was fragile:
	# a hard 12s watchdog on the cloud side blew up on slow box starts,
	# the pyte virtual-screen renderer was needed to recover URLs from
	# claude's cursor-right escape sequences, and the "Press Enter to
	# continue" prompt after the code was never cleared, so the flow
	# silently hung. The non-interactive `claude auth login` subcommand
	# avoids all three problems.
	#
	# State machine (per-attempt):
	#   awaiting_url    → claude prints the OAuth URL.
	#                     We forward it as `claude_login_url`.
	#   awaiting_code   → user pastes via FE → cloud sends
	#                     `claude_login_code` → we write to pty stdin
	#                     and schedule an auto-Enter 2s later.
	#   done            → claude writes ~/.claude.json. The existing
	#                     auth-poll loop on this agent picks it up
	#                     and emits `claude_authed`. Pty exits; we
	#                     clean up.
	#   failed          → bad code, claude exits non-zero, etc. We
	#                     emit `claude_login_failed` with whatever
	#                     stdout we captured for the user to read.
	# ------------------------------------------------------------------

	# Auto-Enter delay after writing the OAuth code: claude usually
	# shows a second "Press Enter to continue" prompt after the code is
	# accepted. 2s matches what telegram_bot.py:ShellSession does and
	# is short enough that the user notices the flow advancing.
	_CLAUDE_LOGIN_AUTO_ENTER_SEC = 2.0

	async def _claude_login_start(self) -> None:
		# Kill any prior in-flight attempt before starting fresh. Two
		# concurrent login pty's would race for the same ~/.claude.json
		# write, and the user can only be in one OAuth flow at a time
		# anyway.
		await self._claude_login_cleanup()

		import pty

		try:
			pid, fd = pty.fork()
		except Exception as e:
			LOG.exception('claude_login: pty.fork failed')
			await self._send(
				{'type': 'claude_login_failed', 'error': f'pty-fork: {e}'}
			)
			return

		if pid == 0:
			# Child. `claude auth login` is the dedicated OAuth subcommand;
			# it prints the URL on stdout and then reads a single line of
			# stdin (the pasted callback URL/code), no slash-command
			# parser needed.
			#
			# COLUMNS=1000 + LINES=50 keep claude from wrapping the
			# OAuth URL (~700 chars) across multiple lines. Without it,
			# claude reads the env at startup and clamps to 80 cols
			# regardless of our later TIOCSWINSZ ioctl.
			try:
				import os as _os

				_os.environ['HOME'] = '/home/bux'
				_os.environ['COLUMNS'] = '1000'
				_os.environ['LINES'] = '50'
				_os.execvp('/usr/bin/claude', ['/usr/bin/claude', 'auth', 'login'])
			except Exception:
				_os._exit(127)

		# Parent. Belt-and-braces: TIOCSWINSZ in case claude re-reads
		# the window size from the pty rather than env.
		try:
			import fcntl
			import struct
			import termios

			fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 50, 1000, 0, 0))
		except Exception:
			LOG.exception('claude_login: TIOCSWINSZ failed (URL may wrap)')

		LOG.info('claude_login: pty forked pid=%s fd=%s', pid, fd)
		self._claude_login_attempt += 1
		self._claude_login_pid = pid
		self._claude_login_fd = fd
		self._claude_login_task = asyncio.create_task(self._claude_login_read_loop(pid, fd))

		await self._send({'type': 'ack', 'cmd': 'claude_login_start', 'ok': True})

	async def _claude_login_read_loop(self, pid: int, fd: int) -> None:
		"""Drain the pty: extract the OAuth URL once, then run to EOF.

		`claude auth login` prints the URL on its own line (no TUI
		cursor escapes splitting it letter-by-letter, unlike interactive
		`/login`), so we only need to ANSI-strip the byte stream and
		regex it against the accumulated buffer. No pyte virtual screen
		needed.
		"""
		import re

		loop = asyncio.get_running_loop()
		url_done = False

		# Strips CSI/OSC/SGR + bare ESC sequences. Plenty for `claude
		# auth login` which only emits color codes around the URL.
		ansi_re = re.compile(
			r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]'
		)
		url_re = re.compile(
			r'https://claude\.(?:com|ai)/(?:cai/)?oauth/authorize\?\S+'
		)
		buf = ''

		try:
			while True:
				try:
					data = await loop.run_in_executor(
						None, _read_with_timeout, fd, 4096, 0.5
					)
				except OSError:
					LOG.info('claude_login: pty closed (pid=%s)', pid)
					# The pty closed: claude either wrote ~/.claude.json and
					# exited cleanly, or bailed without writing credentials.
					# The general auth-poll loop runs on a 15s cadence once
					# the box is past its first minute — too coarse to
					# confirm success inside the cloud's exit watchdog, which
					# is what makes a fast-successful sign-in surface a
					# spurious "Try again". Check directly here at a tight
					# cadence: on success emit `claude_authed` and skip the
					# exited event; on timeout emit `claude_login_exited` so
					# the cloud fails fast. Exactly one terminal event per
					# attempt — no cloud-side race left to lose.
					if await self._claude_login_await_success():
						return
					await self._send({'type': 'claude_login_exited'})
					return
				if data is None:
					# `_read_with_timeout` returns None on poll timeout
					# (no data within 0.5s). Re-check whether this loop
					# still owns the current pty; otherwise exit.
					if self._claude_login_pid != pid:
						return
					continue

				# Append new bytes (stripped of ANSI) to a rolling buffer
				# and search for the URL. Cap buffer so a runaway pty
				# doesn't grow it unbounded — the URL is well under 1KB
				# and shows up in the first second of output.
				buf += ansi_re.sub('', data.decode('utf-8', 'replace'))
				if len(buf) > 32_768:
					buf = buf[-16_384:]

				if not url_done:
					m = url_re.search(buf)
					if m:
						url = m.group(0)
						LOG.info('claude_login: extracted URL (len=%d)', len(url))
						await self._send({'type': 'claude_login_url', 'url': url})
						url_done = True
		except asyncio.CancelledError:
			LOG.info('claude_login: read loop cancelled')
			raise
		except Exception:
			LOG.exception('claude_login: read loop crashed')

	async def _claude_login_await_success(self, deadline_s: float = 15.0) -> bool:
		"""After the login pty closes, watch for credentials to land.

		`claude auth login` writes ~/.claude.json a beat after the user
		pastes a valid code, then exits. The general `_auth_poll_loop`
		runs on a 15s cadence once the box is past its first minute —
		too coarse to confirm success inside the cloud's exit watchdog.
		Poll `claude auth status` directly here every 0.5s for up to
		`deadline_s`.

		Returns True (and emits `claude_authed`) the moment creds appear;
		False on timeout, leaving the caller to emit `claude_login_exited`.
		Keeps `self._authed` in sync so `_auth_poll_loop` doesn't re-emit
		a duplicate `claude_authed` on its next tick.
		"""
		loop = asyncio.get_running_loop()
		deadline = loop.time() + deadline_s
		while loop.time() < deadline:
			if await check_claude_authed():
				self._authed = True
				try:
					await self._send({'type': 'claude_authed'})
					LOG.info('claude_login: success confirmed on pty-exit path')
				except Exception:
					LOG.exception('claude_login: claude_authed emit failed')
				return True
			await asyncio.sleep(0.5)
		LOG.info('claude_login: no credentials within %.0fs of pty exit', deadline_s)
		return False

	async def _claude_login_code(self, *, code: str) -> None:
		"""Pump the OAuth callback code into the pty's stdin."""
		fd = self._claude_login_fd
		if fd is None:
			await self._send(
				{'type': 'ack', 'cmd': 'claude_login_code', 'ok': False, 'error': 'no-session'}
			)
			return
		code = code.strip()
		if not code:
			await self._send(
				{'type': 'ack', 'cmd': 'claude_login_code', 'ok': False, 'error': 'empty-code'}
			)
			return
		try:
			import os as _os

			# `\r\n` covers both readline-style line buffers (which want
			# \n) and raw TTY input (which expects \r as Enter). Sending
			# only \r left claude waiting forever — its readline impl
			# wants the newline.
			_os.write(fd, (code + '\r\n').encode())
		except Exception as e:
			LOG.exception('claude_login: write code failed')
			await self._send(
				{'type': 'ack', 'cmd': 'claude_login_code', 'ok': False, 'error': str(e)}
			)
			return
		# Don't poll for success here — auth_poll_loop already watches
		# `claude auth status` and will emit claude_authed when the
		# token lands. Poke the wakeup event so it rechecks immediately.
		self._auth_wakeup.set()
		# Schedule a blank-Enter ~2s after the code so the second
		# "Press Enter to continue" prompt clears itself. Mirrors
		# auto_enter_after_input_sec=2.0 on the OSS Telegram path,
		# which is what makes that flow reach "login successful".
		# Pin the attempt id so a stale timer from a prior attempt
		# can't write into a recycled fd belonging to a fresh pty.
		asyncio.create_task(
			self._claude_login_auto_enter(fd, self._claude_login_attempt)
		)
		await self._send({'type': 'ack', 'cmd': 'claude_login_code', 'ok': True})

	async def _claude_login_auto_enter(self, fd: int, attempt: int) -> None:
		"""Send a blank `\\n` to the pty after a short delay.

		Best-effort: if the user races us by canceling, or the pty has
		already exited, the write will fail silently and that's fine.

		The `attempt` arg is the monotonic counter captured when we
		scheduled this timer; checking it here means a stale timer from
		attempt N can't fire if attempt N+1 happens to receive the same
		fd from `pty.fork` (kernels reuse closed fd numbers eagerly).
		"""
		await asyncio.sleep(self._CLAUDE_LOGIN_AUTO_ENTER_SEC)
		if self._claude_login_attempt != attempt:
			return
		# Belt-and-braces: also check fd identity. A cancellation between
		# scheduling and firing would have cleared _claude_login_fd to
		# None, so a fd-value compare against `fd` (an int) safely
		# returns False.
		if self._claude_login_fd != fd:
			return
		try:
			import os as _os

			_os.write(fd, b'\n')
		except Exception:
			# Pty closed before we got here — login probably already
			# completed and the auto-poll picked up the new token.
			pass

	async def _claude_login_cancel(self) -> None:
		await self._claude_login_cleanup()
		await self._send({'type': 'ack', 'cmd': 'claude_login_cancel', 'ok': True})

	async def _claude_login_cleanup(self) -> None:
		"""Tear down any in-flight login pty + reader task."""
		import os as _os
		import signal

		task = self._claude_login_task
		pid = self._claude_login_pid
		fd = self._claude_login_fd
		self._claude_login_task = None
		self._claude_login_pid = None
		self._claude_login_fd = None
		if task is not None and not task.done():
			task.cancel()
			try:
				await task
			except (asyncio.CancelledError, Exception):
				pass
		if pid:
			try:
				_os.kill(pid, signal.SIGTERM)
			except ProcessLookupError:
				pass
			except Exception:
				LOG.exception('claude_login: SIGTERM failed pid=%s', pid)
			# Reap so we don't leave a zombie.
			try:
				_os.waitpid(pid, _os.WNOHANG)
			except Exception:
				pass
		if fd is not None:
			try:
				_os.close(fd)
			except Exception:
				pass

	# ------------------------------------------------------------------
	# Codex device-auth (`codex login --device-auth`)
	#
	# Device-auth prints two artifacts on stdout: a fixed URL
	# (https://auth.openai.com/codex/device) and a one-time code (e.g.
	# `WSDR-LFCD`). The user opens the URL in any browser, types the
	# code, authorizes — and the codex CLI on this box exits 0 when
	# OpenAI signals success. Unlike claude there is no callback code
	# the user needs to paste back into the terminal.
	#
	# State machine (per-attempt):
	#   awaiting_url    → codex prints URL + one-time code on stdout.
	#                     We forward both as `codex_login_url` (a single
	#                     event to keep the cloud-side state machine
	#                     simple — claude has separate URL / code
	#                     stages because claude prints them separately).
	#   awaiting_browser_auth → user is on the OpenAI page entering the
	#                     code. Box-agent has nothing to do; the codex
	#                     CLI is blocking on the device-auth poll.
	#   done            → codex CLI exits 0; auth-poll picks up
	#                     `codex_authed` and emits over WS. Pty exits;
	#                     we clean up.
	#   failed          → codex CLI exits non-zero (network error, code
	#                     expired, user denied). We emit
	#                     `codex_login_failed` with the last few stdout
	#                     lines for the user to read.
	# ------------------------------------------------------------------

	async def _codex_login_start(self) -> None:
		# Kill any prior in-flight attempt; running two device-auth flows
		# in parallel makes no sense and the second would just confuse
		# the user when the first is still pending.
		await self._codex_login_cleanup()

		import pty

		try:
			pid, fd = pty.fork()
		except Exception as e:
			LOG.exception('codex_login: pty.fork failed')
			await self._send(
				{'type': 'codex_login_failed', 'error': f'pty-fork: {e}'}
			)
			return

		if pid == 0:
			# Child. `codex login --device-auth` is the headless-friendly
			# flow that prints URL + code instead of trying to open a
			# browser locally (which would never work on a server box).
			#
			# Wide-COLUMNS keeps codex from soft-wrapping its banner /
			# code line at 80 columns; the URL is short enough that
			# wrapping isn't a hazard there but the formatted code line
			# can break across two lines on narrow terminals.
			try:
				import os as _os

				_os.environ['HOME'] = '/home/bux'
				_os.environ['COLUMNS'] = '1000'
				_os.environ['LINES'] = '50'
				_os.execvp(CODEX_BIN, [CODEX_BIN, 'login', '--device-auth'])
			except Exception:
				_os._exit(127)

		# Parent.
		try:
			import fcntl
			import struct
			import termios

			fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 50, 1000, 0, 0))
		except Exception:
			LOG.exception('codex_login: TIOCSWINSZ failed')

		LOG.info('codex_login: pty forked pid=%s fd=%s', pid, fd)
		self._codex_login_attempt += 1
		self._codex_login_pid = pid
		self._codex_login_fd = fd
		self._codex_login_task = asyncio.create_task(
			self._codex_login_read_loop(pid, fd)
		)

		await self._send({'type': 'ack', 'cmd': 'codex_login_start', 'ok': True})

	async def _codex_login_read_loop(self, pid: int, fd: int) -> None:
		"""Drain the pty: extract URL + one-time code, then run to EOF.

		Codex prints the URL and a one-time code on separate lines (the
		exact wording shifts across versions, but the URL always starts
		with `https://auth.openai.com/codex/device` and the code matches
		the device-code shape `[A-Z0-9]{4,}-[A-Z0-9]{4,}` or a single
		6-12 char block). We forward the first match for each as a
		single `codex_login_url` event so the cloud-side state machine
		only has to track one transition.
		"""
		import re

		loop = asyncio.get_running_loop()
		announced = False

		ansi_re = re.compile(
			r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]'
		)
		# Codex's device-auth URL is a known origin/path; matching it
		# explicitly avoids picking up an unrelated http(s) link from a
		# warning / log line.
		url_re = re.compile(r'https://auth\.openai\.com/codex/device\S*')
		# Match either the dashed-block form (WSDR-LFCD) or a single
		# all-caps run (BHFG7M). Same shape `_DEVICE_CODE_RE` uses in the
		# OSS Telegram bot.
		code_re = re.compile(r'\b(?:[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+|[A-Z0-9]{6,12})\b')
		# Last few lines so we have something to surface in the failed
		# message if the CLI exits non-zero.
		recent_lines: list[str] = []
		buf = ''
		url: str | None = None
		code: str | None = None

		try:
			while True:
				try:
					data = await loop.run_in_executor(
						None, _read_with_timeout, fd, 4096, 0.5
					)
				except OSError:
					LOG.info('codex_login: pty closed (pid=%s)', pid)
					# Mirror of the claude_login pty-exit path: confirm
					# success directly here at a tight cadence rather than
					# leaving the cloud to race a 15s auth poll. On success
					# emit `codex_authed` and skip the exited event; on
					# timeout emit `codex_login_exited` so the cloud fails
					# fast. Exactly one terminal event per attempt.
					if await self._codex_login_await_success():
						return
					await self._send({'type': 'codex_login_exited'})
					return
				if data is None:
					if self._codex_login_pid != pid:
						return
					continue

				clean = ansi_re.sub('', data.decode('utf-8', 'replace'))
				buf += clean
				if len(buf) > 32_768:
					buf = buf[-16_384:]
				# Per-line bookkeeping for the failure-tail message.
				while '\n' in clean:
					line, clean = clean.split('\n', 1)
					line = line.strip()
					if line:
						recent_lines.append(line)
						if len(recent_lines) > 8:
							recent_lines = recent_lines[-8:]

				if not announced:
					if url is None:
						m = url_re.search(buf)
						if m:
							url = m.group(0).rstrip('.,)')
					if code is None:
						# Don't grab the first ALL-CAPS token in the
						# buffer — wait until we see context that
						# implies it really is a code. Otherwise we'd
						# match e.g. "OPENAI" from the banner.
						low = buf.lower()
						if (
							'one-time code' in low
							or 'enter this code' in low
							or 'enter the code' in low
							or url is not None
						):
							for m in code_re.finditer(buf):
								tok = m.group(0)
								# Skip obvious banner words. The
								# device-code form (XXXX-YYYY) is
								# vanishingly unlikely to collide.
								if '-' in tok or len(tok) >= 8:
									code = tok
									break
					if url and code:
						LOG.info(
							'codex_login: extracted url=%s code=%s',
							url, code,
						)
						await self._send(
							{
								'type': 'codex_login_url',
								'url': url,
								'code': code,
							}
						)
						announced = True
						# Trim the buffer so we don't keep matching the
						# same URL/code on every tick.
						buf = ''
		except asyncio.CancelledError:
			LOG.info('codex_login: read loop cancelled')
			raise
		except Exception:
			LOG.exception('codex_login: read loop crashed')
			# Best-effort: surface a failed event with whatever we've
			# captured so the FE can show a useful message.
			tail = '\n'.join(recent_lines[-4:]) or 'codex login crashed'
			try:
				await self._send({'type': 'codex_login_failed', 'error': tail})
			except Exception:
				pass

	async def _codex_login_await_success(self, deadline_s: float = 15.0) -> bool:
		"""After the codex device-auth pty closes, watch for the token.

		Mirror of `_claude_login_await_success`. `codex login --device-auth`
		writes its credentials and exits 0 once OpenAI signals the device
		approved. Poll `codex login status` directly here every 0.5s for
		up to `deadline_s` so success is confirmed without racing the
		general `_codex_auth_poll_loop` cadence.

		Returns True (and emits `codex_authed`) on success; False on
		timeout. Keeps `self._codex_authed` in sync so the poll loop
		doesn't re-emit a duplicate.
		"""
		loop = asyncio.get_running_loop()
		deadline = loop.time() + deadline_s
		while loop.time() < deadline:
			if await check_codex_authed():
				self._codex_authed = True
				try:
					await self._send({'type': 'codex_authed'})
					LOG.info('codex_login: success confirmed on pty-exit path')
				except Exception:
					LOG.exception('codex_login: codex_authed emit failed')
				return True
			await asyncio.sleep(0.5)
		LOG.info('codex_login: no credentials within %.0fs of pty exit', deadline_s)
		return False

	async def _codex_login_cancel(self) -> None:
		await self._codex_login_cleanup()
		await self._send({'type': 'ack', 'cmd': 'codex_login_cancel', 'ok': True})

	async def _codex_login_cleanup(self) -> None:
		"""Tear down any in-flight codex_login pty + reader task. Mirror
		of _claude_login_cleanup."""
		import os as _os
		import signal

		task = self._codex_login_task
		pid = self._codex_login_pid
		fd = self._codex_login_fd
		self._codex_login_task = None
		self._codex_login_pid = None
		self._codex_login_fd = None
		if task is not None and not task.done():
			task.cancel()
			try:
				await task
			except (asyncio.CancelledError, Exception):
				pass
		if pid:
			try:
				_os.kill(pid, signal.SIGTERM)
			except ProcessLookupError:
				pass
			except Exception:
				LOG.exception('codex_login: SIGTERM failed pid=%s', pid)
			try:
				_os.waitpid(pid, _os.WNOHANG)
			except Exception:
				pass
		if fd is not None:
			try:
				_os.close(fd)
			except Exception:
				pass

	def stop(self) -> None:
		self._stop.set()


def main() -> int:
	logging.basicConfig(
		level=logging.INFO,
		format='%(asctime)s %(name)s %(levelname)s %(message)s',
	)
	env = load_env() | dict(os.environ)
	cloud_url = env.get('BUX_CLOUD_URL', 'wss://api.browser-use.com')
	box_token = env.get('BUX_BOX_TOKEN', '')
	box_id = env.get('BUX_BOX_ID', '')
	if not box_token or not box_id:
		LOG.error('BUX_BOX_TOKEN and BUX_BOX_ID must be set — idling')
		while True:
			time.sleep(60)
	agent = Agent(cloud_url=cloud_url, box_token=box_token, box_id=box_id)

	loop = asyncio.new_event_loop()
	asyncio.set_event_loop(loop)
	for sig in (signal.SIGINT, signal.SIGTERM):
		loop.add_signal_handler(sig, agent.stop)
	try:
		loop.run_until_complete(agent.run())
	finally:
		loop.close()
	return 0


if __name__ == '__main__':
	sys.exit(main())
