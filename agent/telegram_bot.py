"""Telegram bot running on the box. User-owned — browser-use never touches messages.

Auth: deeplink-based one-shot setup token (jarvis pattern).

Env (from /etc/bux/tg.env):
  TG_BOT_TOKEN     — Telegram bot token from @BotFather
  TG_SETUP_TOKEN   — random secret shown in the deeplink once; burns after first /start

State (on disk):
  /etc/bux/tg-allowed.txt  — space-separated allowed chat_ids
  /etc/bux/tg-state.json   — {offset, per-chat profile overrides}

Flow:
  1. Start → TG_BOT_TOKEN required; begin long-polling getUpdates.
  2. Any message from an un-allowed chat_id is dropped, EXCEPT `/start <token>`
     matching TG_SETUP_TOKEN → binds chat_id, welcomes user.
  3. Once allowed, each message → dispatch to `claude -p "<text>"` with
     BU env forwarded. Output returned as reply.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

LOG = logging.getLogger('bux-tg')

TG_ENV = Path('/etc/bux/tg.env')
BOX_ENV = Path('/etc/bux/env')
BROWSER_ENV = Path('/home/bux/.claude/browser.env')
ALLOWED_FILE = Path('/etc/bux/tg-allowed.txt')
STATE_FILE = Path('/etc/bux/tg-state.json')
QUEUE_FILE = Path('/etc/bux/tg-queue.json')
# Marker for "I've already told the user about this SHA". Lets transient
# bux-tg restarts (systemd flaps, polling backoff) stay silent while
# update-driven restarts (different SHA) announce themselves once.
LAST_ANNOUNCED_SHA = Path('/var/lib/bux/last-announced.sha')
POLL_TIMEOUT = 30
REPLY_MAX = 3500  # TG's limit is 4096; we chunk anyway

# Persisted message queue. Each item is a Job dict:
#   id        — short hex, what /cancel <id> takes
#   chat_id   — TG chat that sent it
#   message_id— TG message id (so we can `reply_to` the original)
#   prompt    — user's text (treat as opaque payload, never logged in event facade)
#   queued_at — unix ts
#   status    — 'queued' | 'in_flight'   ('done' rows are pruned, not kept)
#
# A single worker thread drains this FIFO so claude is never run concurrently
# from the bot side. (box-agent's run_task path still uses claude.lock to
# serialize against the bot — they're separate processes, so the lockfile
# stays as the inter-process gate.)


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


_RE_INLINE_TOKEN = None  # lazy-initialised; built on first call


def _to_tg_markdown_v2(text: str) -> str:
	"""Convert claude's standard markdown to Telegram MarkdownV2.

	Handles the formatting claude actually emits in TG replies:
	  - ```fenced code blocks``` (with optional language tag)
	  - `inline code`
	  - **bold** / __bold__
	  - *italic* / _italic_
	  - [link](url)

	Anything else is treated as plain text and gets the full escape pass.
	We deliberately don't try to be a complete CommonMark parser — that's
	a maintenance trap. The 400-fallback in send() covers our gaps.
	"""
	import re as _re

	# 1) Pull out fenced code blocks first (greedy) so their contents skip
	#    the inline pass entirely. Each block becomes a placeholder.
	blocks: list[str] = []

	def _stash_block(m):
		lang = (m.group(1) or '').strip()
		body = _escape_mdv2_code(m.group(2))
		# MarkdownV2 fenced block: ```lang\n…\n```
		blocks.append(f'```{lang}\n{body}\n```')
		return f'\x00BLOCK{len(blocks) - 1}\x00'

	text = _re.sub(r'```([^\n`]*)\n(.*?)```', _stash_block, text, flags=_re.DOTALL)

	# 2) Pull out inline code (`...`) the same way.
	codes: list[str] = []

	def _stash_code(m):
		codes.append('`' + _escape_mdv2_code(m.group(1)) + '`')
		return f'\x00CODE{len(codes) - 1}\x00'

	text = _re.sub(r'`([^`\n]+)`', _stash_code, text)

	# 3) Inline tokens: bold, italic, links. We rebuild the text by walking
	#    a regex that matches any one of the patterns; everything else is
	#    plain text and goes through the full escape pass.
	pattern = _re.compile(
		r'\*\*(.+?)\*\*'  # **bold**
		r'|__(.+?)__'  # __bold__
		r'|(?<![*\w])\*([^*\n]+?)\*(?!\w)'  # *italic* (avoid matching inside **)
		r'|(?<![_\w])_([^_\n]+?)_(?!\w)'  # _italic_
		r'|\[([^\]\n]+)\]\(([^)\n]+)\)'  # [text](url)
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
		# link: TG escapes inside (...) too, but ) and \ are the special ones
		url = link_url.replace('\\', '\\\\').replace(')', '\\)')
		return '[' + _escape_mdv2_plain(link_text) + '](' + url + ')'

	# Walk the string, alternating between plain runs (escape fully) and
	# matched tokens (escape inner content according to format).
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

	# 4) Restore stashed code spans + blocks (already escaped inside).
	rendered = _re.sub(r'\x00CODE(\d+)\x00', lambda m: codes[int(m.group(1))], rendered)
	rendered = _re.sub(r'\x00BLOCK(\d+)\x00', lambda m: blocks[int(m.group(1))], rendered)
	return rendered


def _chunk_for_telegram(text: str, max_len: int) -> list[str]:
	"""Split on paragraph boundaries when possible so we don't slice
	mid-formatting (which TG would 400 on for MarkdownV2).
	Falls back to char-aligned cut for paragraphs longer than max_len."""
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
	# Any single paragraph longer than max_len gets hard-cut.
	final: list[str] = []
	for c in chunks:
		if len(c) <= max_len:
			final.append(c)
		else:
			for i in range(0, len(c), max_len):
				final.append(c[i : i + max_len])
	return final


def _parse_command(text: str) -> tuple[str | None, str]:
	"""Split a TG message into (command, argument) if it looks like a command.

	Telegram sends `/cmd@botname rest of arg` in group chats — strip the
	`@botname` suffix so the cmd matches whether the bot was invoked by
	bare /cancel or /cancel@bux_abcd1234_bot. Today the bot is bound to a
	1:1 chat by construction (see binding flow), but cheap to be uniform.

	Returns (None, '') for non-command messages so callers can fall through
	to the claude-prompt path without an extra check.

	`split(None, 1)` splits on any whitespace (spaces, tabs, newlines) —
	users on mobile keyboards sometimes paste arguments with hard line
	breaks or autocorrect-inserted nbsp, and `partition(' ')` would let
	those through as part of the command name.
	"""
	if not text or not text.startswith('/'):
		return None, ''
	parts = text.split(None, 1)
	head = parts[0]
	rest = parts[1].strip() if len(parts) > 1 else ''
	cmd, _, _bot = head.partition('@')
	return cmd, rest


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


def _chmod_root_bux_640(path: Path) -> None:
	"""Set `path` to 0o640 root:bux. Raises on failure.

	Used for /etc/bux/tg.env and /etc/bux/tg-allowed.txt — both need to be
	readable by the bux user so the `tg-send` helper can post to TG from
	at/cron jobs (see install.sh). If we can't get the perms right, the
	scheduling path is silently broken and the user has no way to discover
	it short of "my reminder didn't fire". Fail loud so install / first-bind
	surfaces the problem instead.
	"""
	import grp

	bux_gid = grp.getgrnam('bux').gr_gid
	os.chown(path, 0, bux_gid)
	path.chmod(0o640)


def add_allow(chat_id: int) -> None:
	ids = load_allow() | {chat_id}
	ALLOWED_FILE.write_text('\n'.join(str(i) for i in sorted(ids)))
	# 0o640 root:bux — same logic as tg.env: tg-send (running as bux from
	# at/cron) needs to read the bound chat_id. Fail loud rather than
	# silently leave the file unreadable to bux, otherwise scheduled work
	# breaks at fire time with no link back to the binding step.
	_chmod_root_bux_640(ALLOWED_FILE)


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
	# 0o640 root:bux so tg-send (running as bux from at/cron) can read
	# the bot token. Fail loud rather than swallow — a silent chmod miss
	# here means scheduled work breaks at fire time. See box_agent.py
	# _tg_install for the threat-model rationale on widening to bux.
	_chmod_root_bux_640(TG_ENV)


def load_state() -> dict:
	if STATE_FILE.exists():
		try:
			return json.loads(STATE_FILE.read_text())
		except Exception:
			pass
	return {'offset': 0}


def save_state(s: dict) -> None:
	STATE_FILE.write_text(json.dumps(s))


def _session_args() -> list[str]:
	"""Claude CLI args that pin/reuse the box's session.

	First TG message ever: `--session-id <new>` creates the session and writes
	the uuid to disk. Every subsequent message: `--resume <uuid>` picks up the
	same conversation — shared with `bux run` since they read the same file.

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

	TG bot runs as root, box-agent runs as bux. They share this lockfile to
	serialize claude invocations. If TG creates the file first, it'd be
	owned root:root 0644 — which means bux can't open it for writing and
	hits Permission denied. Fix: create with mode 0664 AND chown to bux
	immediately so either side can open it later.

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
		# We just created it as root. Hand it to bux so box-agent (which
		# runs as bux) can open it too. If chown/chmod fails we must NOT
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


# ---------------------------------------------------------------------------
# Persistent FIFO queue for incoming TG messages.
#
# Worker thread pops one job at a time and runs claude. A single worker means
# we never need to lock on the bot side — claude.lock is still held during
# the actual claude invocation, but only as the inter-process gate against
# box-agent's run_task. From the user's TG view, sending 5 messages produces
# 5 immediate "queued #N of M" replies and 5 ordered claude responses.
#
# Persistence: tg-queue.json under /etc/bux (root-owned, mode 600). Atomic
# rename pattern matches browser.env / tg-allowed.txt. On startup we replay
# anything `status=queued` and bury anything `status=in_flight` (assume the
# previous worker crashed mid-task; apologize via reply, don't retry).
# ---------------------------------------------------------------------------


def _new_job_id() -> str:
	"""8 hex chars. Short enough to type into /cancel <id>, long enough that
	collisions across a single user's queue are essentially impossible."""
	return secrets.token_hex(4)


def _load_queue() -> list[dict]:
	if not QUEUE_FILE.exists():
		return []
	try:
		raw = QUEUE_FILE.read_text()
		data = json.loads(raw) if raw.strip() else []
		return data if isinstance(data, list) else []
	except Exception:
		LOG.exception('reading %s failed; treating as empty', QUEUE_FILE)
		return []


def _save_queue(jobs: list[dict]) -> None:
	"""Atomic rename so a crash mid-write can never produce a half-file
	that the next start treats as 'queue is empty'."""
	tmp = QUEUE_FILE.with_suffix('.tmp')
	try:
		tmp.unlink()
	except FileNotFoundError:
		pass
	fd = os.open(
		str(tmp),
		os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_CLOEXEC,
		0o600,
	)
	try:
		os.write(fd, json.dumps(jobs).encode())
	finally:
		os.close(fd)
	tmp.replace(QUEUE_FILE)


# In-memory mirror + lock so the poll thread (enqueue) and worker thread
# (dequeue + status updates) don't race on the JSON file. Source of truth
# is the file on disk; this lock + list is a coalescing layer so we don't
# fsync on every read.
_queue_lock = threading.Lock()
_queue_cv = threading.Condition(_queue_lock)
_queue: list[dict] = []


def _queue_init() -> None:
	"""Called once at bot startup. Loads from disk, scrubs in_flight rows
	(stale from a previous crashed worker) so the worker thread doesn't
	try to re-run a task whose state we've lost."""
	global _queue
	with _queue_lock:
		_queue = _load_queue()
		# in_flight from a previous run = previous worker died mid-task.
		# We can't know if claude finished, partially finished, or never
		# ran. Drop the row; if the user wants to retry, they'll resend.
		dropped: list[dict] = []
		kept: list[dict] = []
		for j in _queue:
			if j.get('status') == 'in_flight':
				dropped.append(j)
			else:
				kept.append(j)
		_queue = kept
		if dropped:
			LOG.warning('dropping %d stale in_flight job(s) from previous run', len(dropped))
		_save_queue(_queue)


def _queue_enqueue(job: dict) -> int:
	"""Append, persist, wake the worker. Returns new queue depth."""
	with _queue_cv:
		_queue.append(job)
		_save_queue(_queue)
		_queue_cv.notify_all()
		return len(_queue)


def _queue_pop_next() -> dict | None:
	"""Block until a queued job appears, then atomically flip it to
	in_flight and return it. The 1s timeout is so the worker can wake
	on shutdown without waiting forever."""
	with _queue_cv:
		while True:
			for j in _queue:
				if j.get('status') == 'queued':
					j['status'] = 'in_flight'
					_save_queue(_queue)
					return j
			_queue_cv.wait(timeout=1.0)


def _queue_remove(job_id: str) -> dict | None:
	"""Remove a queued job by id. Returns the removed row or None.
	in_flight jobs cannot be removed via this path — caller must check
	the returned status."""
	with _queue_cv:
		for i, j in enumerate(_queue):
			if j.get('id') == job_id:
				if j.get('status') != 'queued':
					return j  # caller signals "can't cancel in-flight"
				_queue.pop(i)
				_save_queue(_queue)
				return j
		return None


def _queue_finish(job_id: str) -> None:
	"""Worker calls this when a job's reply has been sent. Removes the
	row; the queue is meant to track pending work, not history."""
	with _queue_cv:
		for i, j in enumerate(_queue):
			if j.get('id') == job_id:
				_queue.pop(i)
				_save_queue(_queue)
				return


def _queue_snapshot(chat_id: int) -> list[dict]:
	"""Read-only view for /queue. Filters to one chat (defense in depth —
	the bot is single-tenant so this is currently a no-op)."""
	with _queue_cv:
		return [dict(j) for j in _queue if j.get('chat_id') == chat_id]


class Bot:
	def __init__(self, token: str, setup_token: str) -> None:
		self.token = token
		self.setup_token = setup_token
		self.api = f'https://api.telegram.org/bot{token}'
		self.client = httpx.Client(timeout=POLL_TIMEOUT + 10)
		self.state = load_state()

	def call(self, method: str, **params) -> dict:
		try:
			r = self.client.post(
				f'{self.api}/{method}', json={k: v for k, v in params.items() if v is not None}
			)
			r.raise_for_status()
			return r.json()
		except httpx.HTTPStatusError as e:
			# Surface the status code so callers can branch on 400 (TG's
			# "can't parse entities" / "message too long" / etc.).
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
		"""Send a message, optionally with MarkdownV2 rendering.

		When `markdown=True` the input is treated as standard markdown
		(bold/italic/code/links). We convert to TG's MarkdownV2 dialect
		(escaping the strict `_*[]()~`>#+-=|{}.!` set outside formatting
		contexts) and send with parse_mode=MarkdownV2. If TG rejects with
		400 (we got the escaping wrong somewhere), we transparently
		fall back to plain text rather than dropping the message.

		Caller default is plain text: bot-authored strings like "chat_id: 12345"
		shouldn't accidentally trip on a stray underscore or asterisk.
		"""
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
					# Escape mistake — re-send as plain text so the user
					# still sees the content.
					LOG.info('MarkdownV2 rejected, falling back to plain text')
					self.call(
						'sendMessage', chat_id=chat_id, text=chunk, reply_to_message_id=reply_to
					)
			else:
				self.call('sendMessage', chat_id=chat_id, text=chunk, reply_to_message_id=reply_to)

	def typing(self, chat_id: int) -> None:
		self.call('sendChatAction', chat_id=chat_id, action='typing')

	# ------------------------------------------------------------------
	# Task dispatch — shell out to `claude -p "<text>"` on the box with
	# BU + profile envs forwarded (same env setup box-agent's run_task uses).
	# Uses --session-id so TG messages share conversational context with
	# `bux run` and previous TG messages (same session UUID on disk).
	# ------------------------------------------------------------------
	def run_task(self, prompt: str) -> str:
		box_env = _read_kv(BOX_ENV)
		browser_env = _read_kv(BROWSER_ENV)
		child_env = {
			**os.environ,
			'HOME': '/home/bux',
			'USER': 'bux',
			'PATH': '/usr/local/bin:/usr/bin:/bin:' + os.environ.get('PATH', ''),
		}
		if box_env.get('BROWSER_USE_API_KEY'):
			child_env['BROWSER_USE_API_KEY'] = box_env['BROWSER_USE_API_KEY']
		if box_env.get('BUX_PROFILE_ID'):
			child_env['BUX_PROFILE_ID'] = box_env['BUX_PROFILE_ID']
			child_env['BU_PROFILE_ID'] = box_env['BUX_PROFILE_ID']
		for k in ('BU_CDP_WS', 'BU_BROWSER_ID'):
			if browser_env.get(k):
				child_env[k] = browser_env[k]

		session_args = _session_args()

		# Cross-process flock shared with box_agent.py's run_task. Acquire
		# blocks, so concurrent messages / `bux run` invocations queue here.
		lock_fd = _acquire_claude_lock()
		try:
			try:
				# Run as bux. We are root (service runs as root so we can sudo).
				proc = subprocess.run(
					[
						'sudo',
						'-u',
						'bux',
						'-H',
						'-E',
						'/usr/bin/claude',
						'-p',
						*session_args,
						'--output-format',
						'text',
						'--permission-mode',
						'bypassPermissions',
						prompt,
					],
					capture_output=True,
					text=True,
					timeout=1800,
					env=child_env,
					cwd='/home/bux',
				)
				out = ((proc.stdout or '') + (proc.stderr or '')).strip()
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

	def _download_telegram_file(self, file_id: str, suffix: str) -> str | None:
		"""Pull a TG attachment to /home/bux/inbox and return the local path.

		TG's two-step model: first getFile to resolve `file_id` → server-side
		`file_path`, then GET https://api.telegram.org/file/bot<token>/<path>
		for the bytes. Files >20MB aren't downloadable via this API; we skip
		those and surface an error to the user upstream.
		"""
		import os as _os

		try:
			info = self.call('getFile', file_id=file_id)
			if not info.get('ok'):
				return None
			file_path = info.get('result', {}).get('file_path', '')
			if not file_path:
				return None
			# bytes endpoint, NOT the bot API endpoint
			url = f'https://api.telegram.org/file/bot{self.token}/{file_path}'
			r = self.client.get(url, timeout=60)
			r.raise_for_status()
			data = r.content
		except Exception:
			LOG.exception('telegram file download failed')
			return None

		inbox = '/home/bux/inbox'
		try:
			_os.makedirs(inbox, exist_ok=True)
			# We're root inside this service; let bux own the tree so claude
			# (running as bux via sudo) can read it.
			_os.chown(inbox, 1001, 1001)
		except Exception:
			LOG.exception('inbox setup failed')
		# Use the message id for uniqueness; suffix carries extension/mime hint.
		fname = f'{int(time.time())}-{file_id[:12]}{suffix}'
		path = f'{inbox}/{fname}'
		try:
			with open(path, 'wb') as f:
				f.write(data)
			_os.chown(path, 1001, 1001)
			_os.chmod(path, 0o644)
		except Exception:
			LOG.exception('writing %s failed', path)
			return None
		return path

	def _extract_attachment(self, msg: dict) -> tuple[str | None, str]:
		"""Return (path-on-disk, prompt-prefix) for any image/doc attachment.

		Caller composes the final prompt as `<prefix> <user-caption>` so claude
		sees both the file reference and any text the user typed.
		"""
		# Photos: msg.photo is a list of size variants. The last entry is
		# the largest (highest-res available). file_unique_id is stable
		# across resends; file_id is the one we feed to getFile.
		photos = msg.get('photo') or []
		if photos:
			file_id = photos[-1].get('file_id')
			if file_id:
				path = self._download_telegram_file(file_id, '.jpg')
				if path:
					return path, f'User sent an image at {path}. '
		# Documents: covers PDFs, images sent as files, etc. Honor the MIME
		# type when present so the saved suffix matches. Falls back to the
		# original filename's extension.
		doc = msg.get('document') or {}
		if doc.get('file_id'):
			fname = doc.get('file_name') or ''
			suffix = ''
			if '.' in fname:
				suffix = '.' + fname.rsplit('.', 1)[1]
			path = self._download_telegram_file(doc['file_id'], suffix or '.bin')
			if path:
				return path, f'User sent a file at {path}. '
		return None, ''

	def handle(self, msg: dict) -> None:
		chat_id = msg['chat']['id']
		text = (msg.get('text') or '').strip()
		# TG puts the caption on `caption` for photo/document/video messages
		# and the text on `text` for plain text. Treat them equivalently —
		# the user typed something, regardless of whether they attached a
		# file too.
		caption = (msg.get('caption') or '').strip()
		if caption and not text:
			text = caption
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

		# Commands. TG sends `/cmd@botname` in group chats so users can
		# disambiguate when multiple bots are present — strip the suffix
		# before matching so the bot still works if someone ever drops it
		# into a group. (Today the binding flow guarantees a 1:1 chat, so
		# in practice this is just defense in depth.)
		cmd, arg = _parse_command(text)
		if cmd in ('/start', '/help'):
			self.send(
				chat_id,
				"Text me anything — I'll run it on your bux.\n"
				'/live — live view URL of the active browser\n'
				'/queue — see pending tasks\n'
				'/cancel — drop everything pending\n'
				'/cancel <id> — drop one pending task\n'
				'/schedules — list reminders / cron jobs (ask claude to cancel)\n'
				'/version — show the bux agent version\n'
				'/update — pull latest code + restart (or /update <branch>)',
			)
			return
		if cmd == '/whoami':
			self.send(chat_id, f'chat_id: {chat_id}')
			return
		if cmd == '/live':
			self.send(chat_id, self._live_url(), reply_to=mid)
			return
		if cmd == '/queue':
			self._cmd_queue(chat_id, mid)
			return
		if cmd == '/cancel':
			self._cmd_cancel(chat_id, mid, arg)
			return
		if cmd in ('/schedules', '/schedule'):
			self._cmd_schedules(chat_id, mid)
			return
		if cmd == '/version':
			self._cmd_version(chat_id, mid)
			return
		if cmd == '/update':
			self._cmd_update(chat_id, mid, arg)
			return

		# Enqueue and acknowledge. Worker thread does the actual claude run
		# and sends the result reply when it's done. Image/document
		# attachments get downloaded synchronously here (small files, <20MB)
		# and reference-prefixed into the prompt so claude can read them.
		attachment_path, attach_prefix = self._extract_attachment(msg)
		if attachment_path is None and not text:
			# Photo/doc download failed; tell the user instead of running
			# claude on an empty prompt.
			has_attachment = bool(msg.get('photo') or msg.get('document'))
			if has_attachment:
				self.send(
					chat_id,
					"Couldn't download that attachment — TG's max for bots is 20 MB. Send a smaller copy or a link?",
					reply_to=mid,
				)
				return
		final_prompt = (attach_prefix + text).strip() or 'Look at the attached file and tell me what it is.'
		job = {
			'id': _new_job_id(),
			'chat_id': chat_id,
			'message_id': mid,
			'prompt': final_prompt,
			'queued_at': time.time(),
			'status': 'queued',
		}
		depth = _queue_enqueue(job)
		# Don't send "on it…" when the queue is empty — TG's "typing…"
		# indicator already conveys "I see you, working on it" and
		# duplicating it just clutters the chat. Only send a visible
		# ack when there's actual queueing happening so the user knows
		# their message landed and how to /cancel it.
		self.typing(chat_id)
		if depth > 1:
			self.send(
				chat_id,
				f'🧠 queued (#{depth}) — id `{job["id"]}`',
				reply_to=mid,
				markdown=True,
			)

	def _cmd_queue(self, chat_id: int, reply_to: int | None) -> None:
		jobs = _queue_snapshot(chat_id)
		if not jobs:
			self.send(chat_id, 'Queue is empty.', reply_to=reply_to)
			return
		lines = ['Pending:']
		for i, j in enumerate(jobs, start=1):
			marker = '▶' if j.get('status') == 'in_flight' else '·'
			# Truncate prompts so a long one doesn't wreck TG formatting.
			preview = (j.get('prompt') or '').strip().splitlines()[0] if j.get('prompt') else ''
			if len(preview) > 60:
				preview = preview[:57] + '…'
			lines.append(f'{marker} `{j["id"]}` — {preview}')
		self.send(chat_id, '\n'.join(lines), reply_to=reply_to, markdown=True)

	def _cmd_cancel(self, chat_id: int, reply_to: int | None, job_id: str) -> None:
		if not job_id:
			# Bare `/cancel` → drop everything queued (but not in_flight, we
			# can't safely kill claude mid-task without losing partial work).
			with _queue_cv:
				before = len(_queue)
				_queue[:] = [
					j for j in _queue if j.get('status') != 'queued' or j.get('chat_id') != chat_id
				]
				dropped = before - len(_queue)
				_save_queue(_queue)
			if dropped == 0:
				self.send(chat_id, 'Nothing queued to cancel.', reply_to=reply_to)
			else:
				self.send(
					chat_id,
					f'Cancelled {dropped} pending task(s). In-flight task continues.',
					reply_to=reply_to,
				)
			return
		removed = _queue_remove(job_id)
		if removed is None:
			self.send(
				chat_id, f'No pending task with id `{job_id}`.', reply_to=reply_to, markdown=True
			)
		elif removed.get('chat_id') != chat_id:
			# Should be unreachable on a single-chat-bound bot, but keep the
			# guard so a future shared-bot mode doesn't leak across chats.
			self.send(
				chat_id, f'No pending task with id `{job_id}`.', reply_to=reply_to, markdown=True
			)
		elif removed.get('status') == 'in_flight':
			self.send(
				chat_id,
				f"Task `{job_id}` is already running and can't be cancelled. It'll finish on its own.",
				reply_to=reply_to,
				markdown=True,
			)
		else:
			self.send(chat_id, f'Cancelled task `{job_id}`.', reply_to=reply_to, markdown=True)

	def _cmd_schedules(self, chat_id: int, reply_to: int | None) -> None:
		"""List the bux user's pending `at` jobs and crontab.

		Read-only. Cancellation is intentionally NOT a bot command — users
		ask claude (\"cancel that 9am reminder\") which has the context to
		map a description to a job id and shell out to atrm / crontab -e.
		Building parallel UX in the bot would just diverge from claude's
		ability to handle ambiguous references like \"the morning email one\".
		"""
		lines: list[str] = []

		# `atq` output: `<id>\t<fire-time>\t<queue>\t<user>` — one row per job.
		# `at -c <id>` dumps the full job script (env, cd, then the actual
		# command on the last non-empty line). We grep the body for the
		# user-facing part so the listing isn't 30 lines of `export PATH=…`.
		try:
			atq_out = subprocess.run(
				['sudo', '-u', 'bux', 'atq'],
				capture_output=True,
				text=True,
				timeout=5,
			).stdout.strip()
		except Exception:
			LOG.exception('atq failed')
			atq_out = ''

		at_rows: list[tuple[str, str, str]] = []  # (id, fire_time, body)
		for row in atq_out.splitlines():
			parts = row.split('\t') if '\t' in row else row.split()
			if not parts:
				continue
			job_id = parts[0]
			# Fire time is everything between id and queue letter — easier
			# to match the second-through-second-to-last fields.
			fire_time = ' '.join(parts[1:-2]) if len(parts) >= 4 else ' '.join(parts[1:])
			body = ''
			try:
				dump = subprocess.run(
					['sudo', '-u', 'bux', 'at', '-c', job_id],
					capture_output=True,
					text=True,
					timeout=5,
				).stdout
				# Last non-empty, non-`}` line is the actual user command.
				for ln in reversed([x for x in dump.splitlines() if x.strip()]):
					if ln.strip().startswith('}'):
						continue
					body = ln.strip()
					break
			except Exception:
				LOG.exception('at -c %s failed', job_id)
			at_rows.append((job_id, fire_time, body))

		if at_rows:
			lines.append('🕒 *Pending reminders*')
			for job_id, fire_time, body in at_rows:
				preview = body if len(body) <= 70 else body[:67] + '…'
				lines.append(f'· `{job_id}` — {fire_time}\n  {preview}')

		# crontab -l prints "no crontab for bux" on stderr + exits 1 when
		# empty, which is fine; we only care about stdout lines starting
		# with a non-comment.
		try:
			cron_out = subprocess.run(
				['sudo', '-u', 'bux', 'crontab', '-l'],
				capture_output=True,
				text=True,
				timeout=5,
			).stdout
		except Exception:
			LOG.exception('crontab -l failed')
			cron_out = ''

		cron_rows = [
			ln for ln in cron_out.splitlines() if ln.strip() and not ln.strip().startswith('#')
		]
		if cron_rows:
			if lines:
				lines.append('')
			lines.append('🔁 *Recurring*')
			for ln in cron_rows:
				preview = ln.strip()
				if len(preview) > 100:
					preview = preview[:97] + '…'
				lines.append(f'· {preview}')

		if not lines:
			self.send(chat_id, 'Nothing scheduled.', reply_to=reply_to)
			return
		lines.append('')
		lines.append('_To cancel: ask claude ("cancel the 9am reminder")._')
		self.send(chat_id, '\n'.join(lines), reply_to=reply_to, markdown=True)

	def _cmd_version(self, chat_id: int, reply_to: int | None) -> None:
		"""Report the agent's git SHA + branch + last-commit-line.

		Lets the user check "what version is my box on" without having
		to ssh in or open the cloud admin UI. Reads straight from the
		cloned OSS repo at /opt/bux/repo.
		"""
		repo = '/opt/bux/repo'
		try:
			sha = subprocess.run(
				['git', '-C', repo, 'rev-parse', '--short', 'HEAD'],
				capture_output=True, text=True, timeout=3,
			).stdout.strip() or 'unknown'
			branch = subprocess.run(
				['git', '-C', repo, 'rev-parse', '--abbrev-ref', 'HEAD'],
				capture_output=True, text=True, timeout=3,
			).stdout.strip() or 'unknown'
			# Last commit summary (one line, no fancy formatting).
			last = subprocess.run(
				['git', '-C', repo, 'log', '-1', '--pretty=%h %s'],
				capture_output=True, text=True, timeout=3,
			).stdout.strip() or '(no log)'
			# Behind/ahead vs origin/<branch>.
			ahead_behind = ''
			rc = subprocess.run(
				['git', '-C', repo, 'fetch', '--quiet', 'origin', branch],
				capture_output=True, timeout=10,
			).returncode
			if rc == 0:
				ab = subprocess.run(
					['git', '-C', repo, 'rev-list', '--left-right', '--count',
					 f'HEAD...origin/{branch}'],
					capture_output=True, text=True, timeout=5,
				).stdout.strip().split()
				if len(ab) == 2:
					ahead, behind = ab
					if behind != '0':
						ahead_behind = f' · *{behind} commits behind* (run /update to catch up)'
					elif ahead != '0':
						ahead_behind = f' · {ahead} commits ahead of origin'
		except Exception:
			LOG.exception('/version failed')
			self.send(chat_id, 'Could not read version.', reply_to=reply_to)
			return
		body = (
			f'*bux* on `{branch}`\n'
			f'`{sha}` — {last}{ahead_behind}\n\n'
			'_Source: github.com/browser-use/bux_'
		)
		self.send(chat_id, body, reply_to=reply_to, markdown=True)

	def _cmd_update(self, chat_id: int, reply_to: int | None, branch: str) -> None:
		"""Pull latest agent code from OSS and restart services.

		Branch defaults to whatever the box is tracking (`main` for now).
		Pass `/update <branch>` to switch tracks (e.g. /update stable).

		The restart kills this very process, so we send the ack BEFORE
		invoking bootstrap.sh. The new agent comes up within ~10s and
		the user's next message lands fine.
		"""
		repo = '/opt/bux/repo'
		target = (branch or '').strip() or subprocess.run(
			['git', '-C', repo, 'rev-parse', '--abbrev-ref', 'HEAD'],
			capture_output=True, text=True, timeout=3,
		).stdout.strip() or 'main'

		# Acknowledge first so the user gets a reply even if bootstrap
		# kills us mid-flight.
		self.send(
			chat_id,
			f'⏳ Updating to latest `{target}`…',
			reply_to=reply_to,
			markdown=True,
		)

		try:
			# Widen the fetch refspec to all branches if it isn't already.
			# install.sh clones with --branch main, leaving a single-branch
			# remote that can't reach feature branches by name. Idempotent.
			subprocess.run(
				['git', '-C', repo, 'config', '--replace-all',
				 'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*'],
				capture_output=True, text=True, timeout=5,
			)
			# Explicit refspec form so this works on boxes that haven't
			# run the widening step yet (older bux installs).
			r = subprocess.run(
				['git', '-C', repo, 'fetch', '--prune', 'origin',
				 f'+refs/heads/{target}:refs/remotes/origin/{target}'],
				capture_output=True, text=True, timeout=60,
			)
			if r.returncode != 0:
				self.send(chat_id, f'❌ git fetch failed: {r.stderr[:300]}', reply_to=reply_to)
				return
			# checkout -B (not reset --hard) so HEAD's symbolic-ref points
			# at the requested branch. reset --hard moves whatever-branch-
			# we're-on to the target commit without switching branches —
			# so /version still reports the old branch name after update.
			r = subprocess.run(
				['git', '-C', repo, 'checkout', '-B', target, '--track', f'origin/{target}'],
				capture_output=True, text=True, timeout=15,
			)
			if r.returncode != 0:
				self.send(chat_id, f'❌ git checkout failed: {r.stderr[:300]}', reply_to=reply_to)
				return
			new_sha = subprocess.run(
				['git', '-C', repo, 'rev-parse', '--short', 'HEAD'],
				capture_output=True, text=True, timeout=3,
			).stdout.strip()
			# Tell the user the new SHA *now*, before bootstrap kills us.
			self.send(
				chat_id,
				f'✓ Pulled `{new_sha}`. Restarting bux…',
				reply_to=reply_to,
				markdown=True,
			)
			# Run bootstrap.sh as root. bux-tg.service runs as root so
			# this is direct — no sudo needed. bootstrap.sh re-applies
			# systemd units / cron / pip deps, then restarts box-agent
			# AND bux-tg (since both are active). This Popen call is
			# fire-and-forget; the restart kills us before we'd wait.
			subprocess.Popen(
				['/bin/bash', f'{repo}/agent/bootstrap.sh'],
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
			)
		except Exception as e:
			LOG.exception('/update failed')
			self.send(chat_id, f'❌ update failed: {e}', reply_to=reply_to)

	def queue_worker(self) -> None:
		"""Single drain loop. Pops one job at a time, runs claude, replies.
		The lockfile is still held during run_task so the box-agent's own
		shell sessions and run_task path can't interleave."""
		LOG.info('bux-tg queue worker starting')
		while True:
			job = _queue_pop_next()
			if job is None:  # only happens during shutdown
				return
			# Pyright sees dict.get() as Unknown|None. We control every
			# enqueue path (handle()) so chat_id is always a populated int
			# at this point, but be defensive: a future caller, a manually
			# edited tg-queue.json, or a forward-incompatible field rename
			# shouldn't crash the worker on the next pop. Skip the row and
			# move on.
			chat_id_raw = job.get('chat_id')
			mid_raw = job.get('message_id')
			job_id = str(job.get('id') or '?')
			if not isinstance(chat_id_raw, int):
				LOG.warning('queue job %s missing chat_id; skipping', job_id)
				_queue_finish(job_id)
				continue
			chat_id: int = chat_id_raw
			mid: int | None = mid_raw if isinstance(mid_raw, int) else None
			prompt = str(job.get('prompt') or '')
			try:
				self.typing(chat_id)
				result = self.run_task(prompt)
				self.send(chat_id, result, reply_to=mid, markdown=True)
			except Exception as e:
				LOG.exception('queue job %s failed', job_id)
				try:
					self.send(chat_id, f'❌ task failed: {e}', reply_to=mid)
				except Exception:
					LOG.exception('also failed to send error reply')
			finally:
				_queue_finish(job_id)

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
		"""Run handle() off the poll loop. handle() itself is fast (just
		parses, enqueues, sends an ack) but a few commands like /live do
		external HTTP calls that we don't want sitting in front of the
		next getUpdates poll."""
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
					threading.Thread(
						target=self._handle_in_thread,
						args=(msg,),
						daemon=True,
					).start()
			except httpx.HTTPError:
				LOG.exception('poll failed; sleep 5s')
				time.sleep(5)
			except Exception:
				LOG.exception('unexpected; sleep 5s')
				time.sleep(5)


def _announce_online_if_new_sha(bot: Bot) -> None:
	"""Tell every bound chat "✓ bux online (sha=…)" — but only once per SHA.

	Why a marker file instead of always-announce: bux-tg gets restarted by
	plenty of things that aren't user-initiated updates — systemd flaps,
	long-poll backoff escapes, the post-update agent restart itself. A
	naive "always send on startup" would spam the chat every time the
	service blips. So we cache the last SHA we announced in
	/var/lib/bux/last-announced.sha; same SHA → silent restart, different
	SHA (or first ever boot) → one message.

	No-op if no chat is bound yet (fresh install pre-/start). Best-effort
	throughout — failure to announce must never block bot startup, since
	the announcement is courtesy and the bot is the recovery surface.
	"""
	try:
		repo = '/opt/bux/repo'
		sha = subprocess.run(
			['git', '-C', repo, 'rev-parse', '--short', 'HEAD'],
			capture_output=True, text=True, timeout=3,
		).stdout.strip()
		if not sha:
			return
		try:
			last = LAST_ANNOUNCED_SHA.read_text().strip()
		except FileNotFoundError:
			last = ''
		if sha == last:
			return
		branch = subprocess.run(
			['git', '-C', repo, 'rev-parse', '--abbrev-ref', 'HEAD'],
			capture_output=True, text=True, timeout=3,
		).stdout.strip() or '?'
		chats = load_allow()
		text = f'✓ bux online (sha={sha}, branch={branch})'
		for chat_id in chats:
			try:
				bot.send(chat_id=chat_id, text=text)
			except Exception:
				LOG.exception('online-announce send failed for chat %s', chat_id)
		# Write only after at least one send attempt, so a transient TG
		# outage doesn't permanently suppress the announcement.
		LAST_ANNOUNCED_SHA.parent.mkdir(parents=True, exist_ok=True)
		LAST_ANNOUNCED_SHA.write_text(sha + '\n')
	except Exception:
		LOG.exception('announce_online_if_new_sha failed')


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

	# Hydrate the on-disk queue, drop any stale in_flight rows from a
	# previous crash, then spin up the single worker that drains it.
	# Worker is daemon=True so process exit doesn't block on it; an
	# in-flight claude invocation gets SIGTERM via subprocess timeout.
	_queue_init()
	bot = Bot(token, setup_token)
	threading.Thread(target=bot.queue_worker, name='bux-tg-queue', daemon=True).start()
	# Announce *before* poll_loop so the user gets the "back online" ping
	# immediately on restart, not whenever the first long-poll completes.
	_announce_online_if_new_sha(bot)
	bot.poll_loop()
	return 0


if __name__ == '__main__':
	sys.exit(main())
