"""Telegram bot running on the box. User-owned — browser-use never touches messages.

Auth: first-message-wins binding. The first chat_id that messages the freshly
installed bot becomes the owner; every subsequent chat is silently dropped.
TG_SETUP_TOKEN is a one-shot guard: while it's set, the first message binds;
after bind it's wiped from disk so a breach/backup leak can't bind a new chat.

Env (from /etc/bux/tg.env):
  TG_BOT_TOKEN     — Telegram bot token from @BotFather
  TG_SETUP_TOKEN   — random secret, present until the first chat binds

State (on disk):
  /etc/bux/tg-allowed.txt  — newline-separated allowed chat_ids (mode 600)
  /etc/bux/tg-state.json   — {offset: <last-update_id>}                (mode 600)

Flow:
  1. Start → TG_BOT_TOKEN required; begin long-polling getUpdates.
  2. First message from any chat while TG_SETUP_TOKEN is present → bind.
     All subsequent messages from other chats are silently dropped.
  3. Once bound, each message dispatches to `claude -p --resume <uuid>` so the
     whole conversation shares memory. Serialized via /home/bux/.bux/claude.lock.
  4. Commands: /start, /help, /whoami, /live (browser live-view URL).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

LOG = logging.getLogger('bux-tg')

TG_ENV = Path('/etc/bux/tg.env')
BOX_ENV = Path('/etc/bux/env')
BROWSER_ENV = Path('/home/bux/.claude/browser.env')
ALLOWED_FILE = Path('/etc/bux/tg-allowed.txt')
STATE_FILE = Path('/etc/bux/tg-state.json')
POLL_TIMEOUT = 30
REPLY_MAX = 3500  # TG's limit is 4096; we chunk anyway


# Telegram MarkdownV2 has a strict escape set: every one of these chars,
# anywhere outside an entity, must be backslash-escaped. Inside `code` and
# ```pre``` only ` and \ are special. https://core.telegram.org/bots/api#markdownv2-style
_MDV2_SPECIALS = r'_*[]()~`>#+-=|{}.!'
_MDV2_ESCAPE = {c: '\\' + c for c in _MDV2_SPECIALS}


def _escape_mdv2_plain(s: str) -> str:
	"""Backslash-escape every MarkdownV2 special char in plain text."""
	return ''.join(_MDV2_ESCAPE.get(c, c) for c in s)


def _escape_mdv2_code(s: str) -> str:
	"""Inside code spans / blocks, only ` and \\ need escaping."""
	return s.replace('\\', '\\\\').replace('`', '\\`')


def _to_tg_markdown_v2(text: str) -> str:
	"""Convert claude's standard markdown to Telegram MarkdownV2.

	Handles the formatting claude actually emits: fenced code blocks,
	inline code, **bold** / __bold__, *italic* / _italic_, [link](url).
	Anything else is plain text and gets the full escape pass. The
	400-fallback in send() covers gaps in this converter.
	"""
	import re as _re

	blocks: list[str] = []

	def _stash_block(m):
		lang = (m.group(1) or '').strip()
		body = _escape_mdv2_code(m.group(2))
		blocks.append(f'```{lang}\n{body}\n```')
		return f'\x00BLOCK{len(blocks) - 1}\x00'

	text = _re.sub(r'```([^\n`]*)\n(.*?)```', _stash_block, text, flags=_re.DOTALL)

	codes: list[str] = []

	def _stash_code(m):
		codes.append('`' + _escape_mdv2_code(m.group(1)) + '`')
		return f'\x00CODE{len(codes) - 1}\x00'

	text = _re.sub(r'`([^`\n]+)`', _stash_code, text)

	pattern = _re.compile(
		r'\*\*(.+?)\*\*'
		r'|__(.+?)__'
		r'|(?<![*\w])\*([^*\n]+?)\*(?!\w)'
		r'|(?<![_\w])_([^_\n]+?)_(?!\w)'
		r'|\[([^\]\n]+)\]\(([^)\n]+)\)'
	)

	def _render(m):
		bold = m.group(1) or m.group(2)
		italic = m.group(3) or m.group(4)
		link_text = m.group(5)
		link_url = m.group(6)
		if bold is not None:
			return '*' + _escape_mdv2_plain(bold) + '*'
		if italic is not None:
			return '_' + _escape_mdv2_plain(italic) + '_'
		url = link_url.replace('\\', '\\\\').replace(')', '\\)')
		return '[' + _escape_mdv2_plain(link_text) + '](' + url + ')'

	out: list[str] = []
	pos = 0
	for m in pattern.finditer(text):
		if m.start() > pos:
			out.append(_escape_mdv2_plain(text[pos : m.start()]))
		out.append(_render(m))
		pos = m.end()
	if pos < len(text):
		out.append(_escape_mdv2_plain(text[pos:]))
	rendered = ''.join(out)

	rendered = _re.sub(r'\x00CODE(\d+)\x00', lambda m: codes[int(m.group(1))], rendered)
	rendered = _re.sub(r'\x00BLOCK(\d+)\x00', lambda m: blocks[int(m.group(1))], rendered)
	return rendered


def _chunk_for_telegram(text: str, max_len: int) -> list[str]:
	"""Split on paragraph boundaries when possible so we don't slice
	mid-formatting (TG would 400 on that for MarkdownV2)."""
	if len(text) <= max_len:
		return [text or ' ']
	chunks: list[str] = []
	current = ''
	for para in text.split('\n\n'):
		if not current:
			current = para
		elif len(current) + 2 + len(para) <= max_len:
			current = current + '\n\n' + para
		else:
			chunks.append(current)
			current = para
	if current:
		chunks.append(current)
	final: list[str] = []
	for c in chunks:
		if len(c) <= max_len:
			final.append(c)
		else:
			for i in range(0, len(c), max_len):
				final.append(c[i : i + max_len])
	return final


def _read_kv(path: Path) -> dict[str, str]:
	if not path.exists():
		return {}
	out: dict[str, str] = {}
	for line in path.read_text().splitlines():
		line = line.strip()
		if not line or line.startswith('#') or '=' not in line:
			continue
		k, v = line.split('=', 1)
		out[k.strip()] = v.strip().strip('"').strip("'")
	return out


def load_allow() -> set[int]:
	if not ALLOWED_FILE.exists():
		return set()
	return {int(x) for x in ALLOWED_FILE.read_text().split() if x.strip()}


def add_allow(chat_id: int) -> None:
	ids = load_allow() | {chat_id}
	ALLOWED_FILE.write_text('\n'.join(str(i) for i in sorted(ids)))
	ALLOWED_FILE.chmod(0o600)


def burn_setup_token() -> None:
	"""Remove TG_SETUP_TOKEN from /etc/bux/tg.env after first successful bind.

	Single-use: once a chat_id is bound, the setup token is useless and should
	not sit on disk. Anyone who later reads tg.env (breach, backup leak, etc.)
	can't bind a new chat.
	"""
	if not TG_ENV.exists():
		return
	kept: list[str] = []
	for line in TG_ENV.read_text().splitlines():
		if line.strip().startswith('TG_SETUP_TOKEN='):
			continue
		kept.append(line)
	TG_ENV.write_text('\n'.join(kept) + ('\n' if kept else ''))
	try:
		TG_ENV.chmod(0o600)
	except Exception:
		pass


def load_state() -> dict:
	if STATE_FILE.exists():
		try:
			return json.loads(STATE_FILE.read_text())
		except Exception:
			pass
	return {'offset': 0}


def save_state(s: dict) -> None:
	# 0600 — state lives in /etc/bux but there's no reason for non-root to read it.
	STATE_FILE.write_text(json.dumps(s))
	try:
		STATE_FILE.chmod(0o600)
	except Exception:
		pass


def _session_args() -> list[str]:
	"""Claude CLI args that pin/reuse this box's claude session.

	First message ever: `--session-id <new>` creates the session and writes
	the uuid to /home/bux/.bux/session. Every subsequent message: `--resume
	<uuid>` picks up the same conversation — so the whole chat history stays
	coherent across messages.

	This runs as root (bux-tg.service). `/home/bux` is writable by the `bux`
	user, so any naive open()/chown() on paths under it can be hijacked via
	a planted symlink (e.g. symlink /home/bux/.bux/session → /etc/shadow,
	root overwrites shadow on first TG message). We use O_NOFOLLOW on open
	and lchown() for the chown to prevent that.
	"""
	path = '/home/bux/.bux/session'
	dir_path = os.path.dirname(path)

	# Read existing session. O_NOFOLLOW → ELOOP if `path` is a symlink.
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

	# Ensure the directory exists and isn't itself a symlink.
	os.makedirs(dir_path, exist_ok=True)
	if os.path.islink(dir_path):
		raise RuntimeError(f'{dir_path} is a symlink; refusing to write session')

	sid = str(uuid.uuid4())
	# O_NOFOLLOW refuses to open through a pre-existing symlink at `path`.
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
	# lchown() never follows symlinks. Redundant after O_NOFOLLOW but cheap.
	try:
		import pwd

		bux = pwd.getpwnam('bux')
		os.lchown(path, bux.pw_uid, bux.pw_gid)
	except Exception:
		LOG.exception('chown %s failed', path)
	LOG.info('created new bux claude session_id=%s', sid)
	return ['--session-id', sid]


_CLAUDE_LOCK_PATH = '/home/bux/.bux/claude.lock'


def _open_lockfile() -> int:
	"""Open the cross-process lockfile symlink-safely.

	TG bot runs as root, any other claude-invoking service (box-agent etc.)
	runs as bux. They share this lockfile to serialize claude invocations.
	If TG creates the file first, it would be owned root:root 0644 — which
	means bux can't open it for writing and hits Permission denied. Fix:
	create with mode 0664 AND chown to bux immediately so either side can
	open it later.

	/home/bux is bux-writable — a symlink at the lock path pointing at
	/etc/sudoers would otherwise let a compromised bux trick root into
	creating/touching arbitrary files. O_NOFOLLOW rejects symlinks at the
	final component.
	"""
	dir_path = os.path.dirname(_CLAUDE_LOCK_PATH)
	os.makedirs(dir_path, exist_ok=True)
	if os.path.islink(dir_path):
		raise RuntimeError(f'{dir_path} is a symlink; refusing to open lock')
	# Try to create the file exclusively so we know for certain whether WE
	# made it (and therefore must hand it to bux) vs. opening one a peer
	# already owns. A racy existsthen-open check could otherwise either
	# miss the chown or unlink a file another process is actively flocking.
	created_by_us = False
	try:
		fd = os.open(
			_CLAUDE_LOCK_PATH,
			os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
			0o664,
		)
		created_by_us = True
	except FileExistsError:
		fd = os.open(_CLAUDE_LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW)

	if created_by_us:
		# We just created it as root. Hand it to bux so peer services (which
		# run as bux) can open it too. If chown/chmod fails we must NOT
		# return silently — a root-owned lockfile is the original bug.
		# Don't unlink on failure: another caller in a concurrent process
		# could already be locking the same inode. Just close + re-raise.
		try:
			import pwd

			bux = pwd.getpwnam('bux')
			os.fchown(fd, bux.pw_uid, bux.pw_gid)
			os.fchmod(fd, 0o664)
		except Exception:
			LOG.exception('chown %s failed; leaving file in place', _CLAUDE_LOCK_PATH)
			os.close(fd)
			raise
	return fd


def _acquire_claude_lock() -> int:
	"""Cross-process exclusive lock shared with box_agent.py's run_task.

	Returns the fd — caller must fcntl.LOCK_UN + os.close() it when done.
	Blocks until the lock is free.
	"""
	fd = _open_lockfile()
	fcntl.flock(fd, fcntl.LOCK_EX)
	return fd


def _release_claude_lock(fd: int) -> None:
	try:
		fcntl.flock(fd, fcntl.LOCK_UN)
	finally:
		os.close(fd)


def _try_acquire_claude_lock() -> int | None:
	"""Non-blocking variant. Returns fd if acquired, None if already held."""
	fd = _open_lockfile()
	try:
		fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		return fd
	except BlockingIOError:
		os.close(fd)
		return None


class Bot:
	def __init__(self, token: str, setup_token: str) -> None:
		self.token = token
		self.setup_token = setup_token
		self.api = f'https://api.telegram.org/bot{token}'
		self.client = httpx.Client(timeout=POLL_TIMEOUT + 10)
		self.state = load_state()
		# Bounded worker pool so a message storm / reconnect replay can't
		# explode into unbounded thread growth. Claude itself is serialized
		# by flock, so most threads just sit in the queue blocked on the lock;
		# the cap only matters for bursty /live or /help traffic.
		self.workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix='bux-tg')

	def call(self, method: str, **params) -> dict:
		try:
			r = self.client.post(
				f'{self.api}/{method}', json={k: v for k, v in params.items() if v is not None}
			)
			r.raise_for_status()
			return r.json()
		except httpx.HTTPStatusError as e:
			body = ''
			try:
				body = e.response.text
			except Exception:
				pass
			LOG.warning('%s failed: %s body=%s', method, e, body[:300])
			return {'ok': False, 'error_code': e.response.status_code, 'description': body}
		except Exception as e:
			LOG.warning('%s failed: %s', method, e)
			return {'ok': False}

	def send(
		self,
		chat_id: int,
		text: str,
		reply_to: int | None = None,
		markdown: bool = False,
	) -> None:
		"""Send a message, optionally with MarkdownV2 rendering. Falls
		back to plain text if TG rejects the escaping with HTTP 400."""
		chunks = (
			_chunk_for_telegram(text, REPLY_MAX)
			if markdown
			else [text[i : i + REPLY_MAX] or ' ' for i in range(0, max(len(text), 1), REPLY_MAX)]
		)
		for chunk in chunks:
			if markdown:
				rendered = _to_tg_markdown_v2(chunk)
				resp = self.call(
					'sendMessage',
					chat_id=chat_id,
					text=rendered,
					reply_to_message_id=reply_to,
					parse_mode='MarkdownV2',
				)
				if resp.get('ok') is False and resp.get('error_code') == 400:
					LOG.info('MarkdownV2 rejected, falling back to plain text')
					self.call('sendMessage', chat_id=chat_id, text=chunk, reply_to_message_id=reply_to)
			else:
				self.call('sendMessage', chat_id=chat_id, text=chunk, reply_to_message_id=reply_to)

	def typing(self, chat_id: int) -> None:
		self.call('sendChatAction', chat_id=chat_id, action='typing')

	# ------------------------------------------------------------------
	# Task dispatch — shell out to `claude -p "<text>"` on the box with
	# BU + profile envs forwarded. Uses --resume against a persistent per-box
	# session UUID so every message continues the same conversation.
	# ------------------------------------------------------------------
	def run_task(self, prompt: str) -> str:
		box_env = _read_kv(BOX_ENV)
		browser_env = _read_kv(BROWSER_ENV)

		# sudo strips the environment by default; `-E` only helps if sudoers
		# has `env_keep` entries for the specific vars. We don't want to
		# require a sudoers drop-in on OSS installs, so we pass each var
		# explicitly via `sudo VAR=value …` — that's always forwarded.
		forwarded: dict[str, str] = {
			'HOME': '/home/bux',
			'USER': 'bux',
			'PATH': '/usr/local/bin:/usr/bin:/bin',
		}
		if box_env.get('BROWSER_USE_API_KEY'):
			forwarded['BROWSER_USE_API_KEY'] = box_env['BROWSER_USE_API_KEY']
		if box_env.get('BUX_PROFILE_ID'):
			forwarded['BUX_PROFILE_ID'] = box_env['BUX_PROFILE_ID']
			forwarded['BU_PROFILE_ID'] = box_env['BUX_PROFILE_ID']
		for k in ('BU_CDP_WS', 'BU_BROWSER_ID', 'BU_BROWSER_EXPIRES_AT'):
			if browser_env.get(k):
				forwarded[k] = browser_env[k]

		session_args = _session_args()

		# Cross-process flock shared with any other claude invoker on this
		# box. Acquire blocks, so concurrent messages queue cleanly.
		lock_fd = _acquire_claude_lock()
		try:
			try:
				# Run as bux. We are root (service runs as root so we can sudo).
				# `sudo VAR=val ...` sets env for the child without needing any
				# sudoers env_keep configuration.
				cmd = ['sudo', '-u', 'bux', '-H']
				cmd += [f'{k}={v}' for k, v in forwarded.items()]
				cmd += [
					'/usr/bin/claude',
					'-p',
					*session_args,
					'--output-format',
					'text',
					'--permission-mode',
					'bypassPermissions',
					prompt,
				]
				proc = subprocess.run(
					cmd,
					capture_output=True,
					text=True,
					timeout=1800,
					cwd='/home/bux',
				)
				out = (proc.stdout or '').strip()
				if not out and proc.returncode != 0:
					# Bubble stderr only when claude actually failed — keeps
					# normal replies clean, still surfaces diagnostics to TG
					# when something broke. Set BUX_DEBUG=1 to always include.
					err = (proc.stderr or '').strip()
					return err or f'(no output; rc={proc.returncode})'
				if os.environ.get('BUX_DEBUG') and proc.stderr:
					out = f'{out}\n\n--stderr--\n{proc.stderr.strip()}'
				return out or '(no output)'
			except subprocess.TimeoutExpired:
				return '⏱ Timed out after 30 min.'
			except Exception as e:
				return f'❌ task failed: {e}'
		finally:
			_release_claude_lock(lock_fd)

	# ------------------------------------------------------------------
	def _bind_chat(self, chat_id: int) -> None:
		"""Register chat_id, burn the setup_token, welcome the user."""
		add_allow(chat_id)
		burn_setup_token()
		self.setup_token = ''
		LOG.info('authorized chat_id=%s', chat_id)
		self.send(
			chat_id,
			'✓ Linked.\n\n'
			f'Chat id: {chat_id}\n\n'
			'🔒 This bot is now locked to this chat only. '
			'Every other chat is silently dropped — even if someone '
			'somehow discovers the bot handle.\n\n'
			"Text me anything and I'll run it on your bux.",
		)

	def handle(self, msg: dict) -> None:
		chat_id = msg['chat']['id']
		text = (msg.get('text') or '').strip()
		mid = msg.get('message_id')
		allow = load_allow()

		# Binding path — first-come-first-served.
		#
		# The bot was created seconds ago with a randomized, unenumerable
		# username (`bux_<8hex>_bot`, 62^8 ≈ 2×10^14 search space). Only the
		# user's local skill knows the name. So the first chat to message
		# this bot is, by construction, the owner.
		#
		# Once we have an allow-list entry, setup_token is burned and every
		# other chat_id is silently dropped forever.
		if chat_id not in allow:
			if not self.setup_token:
				# Already bound somewhere else. Strangers get nothing.
				LOG.info('dropping msg from chat_id=%s (already bound)', chat_id)
				return
			# First message from anyone while token still active → bind.
			LOG.info('binding chat_id=%s (first-message wins)', chat_id)
			self._bind_chat(chat_id)
			return

		# Commands.
		if text in ('/start', '/help'):
			self.send(
				chat_id,
				"Text me anything — I'll run it on your bux.\n"
				'/live — live view URL of the active browser',
			)
			return
		if text == '/whoami':
			self.send(chat_id, f'chat_id: {chat_id}')
			return
		if text == '/live':
			self.send(chat_id, self._live_url(), reply_to=mid)
			return

		# Let the user know if their message is going to wait. A tiny race
		# here is harmless — the lock attempt inside run_task is blocking.
		probe = _try_acquire_claude_lock()
		if probe is None:
			self.send(chat_id, '🧠 queued — one task ahead of you…', reply_to=mid)
		else:
			_release_claude_lock(probe)
			self.send(chat_id, '🧠 on it…', reply_to=mid)
		self.typing(chat_id)
		result = self.run_task(text)
		# claude's reply is markdown — render bold/code/links instead of
		# showing literal asterisks. Falls back to plain text if TG
		# rejects the MarkdownV2 escaping.
		self.send(chat_id, result, reply_to=mid, markdown=True)

	# ------------------------------------------------------------------
	def _live_url(self) -> str:
		"""Return the live-view URL of the box's current browser session."""
		box_env = _read_kv(BOX_ENV)
		browser_env = _read_kv(BROWSER_ENV)
		api_key = box_env.get('BROWSER_USE_API_KEY')
		browser_id = browser_env.get('BU_BROWSER_ID')
		if not api_key:
			return '❌ no BROWSER_USE_API_KEY on this box'
		if not browser_id:
			return '❌ no active browser yet — keeper may still be starting'
		try:
			r = httpx.get(
				f'https://api.browser-use.com/api/v3/browsers/{browser_id}',
				headers={'X-Browser-Use-API-Key': api_key},
				timeout=10,
			)
			r.raise_for_status()
			live = r.json().get('liveUrl')
			if not live:
				return '❌ browser has no liveUrl (session may be stale)'
			return f'🖥 {live}'
		except Exception as e:
			return f'❌ live-url lookup failed: {e}'

	def _handle_in_thread(self, msg: dict) -> None:
		"""Run handle() off the poll loop so claude invocations don't block
		/live, /help, or the next `getUpdates`. Claude itself is serialized
		via the flock inside run_task, so multiple concurrent threads queue
		cleanly and the second one can report "queued" to the user."""
		try:
			self.handle(msg)
		except Exception:
			LOG.exception('handle failed')

	def poll_loop(self) -> None:
		LOG.info('bux-tg starting poll loop')
		while True:
			try:
				params = {'timeout': POLL_TIMEOUT}
				if self.state.get('offset'):
					params['offset'] = self.state['offset'] + 1
				data = self.call('getUpdates', **params)
				updates = data.get('result', [])
				if updates:
					self.state['offset'] = max(u['update_id'] for u in updates)
					save_state(self.state)
				for u in updates:
					msg = u.get('message') or u.get('edited_message')
					if not msg:
						continue
					self.workers.submit(self._handle_in_thread, msg)
			except httpx.HTTPError:
				LOG.exception('poll failed; sleep 5s')
				time.sleep(5)
			except Exception:
				LOG.exception('unexpected; sleep 5s')
				time.sleep(5)


def main() -> int:
	logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
	env = _read_kv(TG_ENV)
	token = env.get('TG_BOT_TOKEN') or os.environ.get('TG_BOT_TOKEN')
	setup_token = env.get('TG_SETUP_TOKEN') or os.environ.get('TG_SETUP_TOKEN', '')
	if not token:
		LOG.error('TG_BOT_TOKEN missing in %s', TG_ENV)
		return 1
	signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
	signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
	Bot(token, setup_token).poll_loop()
	return 0


if __name__ == '__main__':
	sys.exit(main())
