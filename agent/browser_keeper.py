#!/usr/bin/env python3
"""browser-keeper — maintains a long-lived Browser Use Cloud browser bound to
the box's profile, so every task skips the 20-30s cold-start.

Writes /home/bux/.claude/browser.env (mode 640, owner bux:bux) on each rotation:
    BU_PROFILE_ID=<id>
    BU_BROWSER_ID=<id>
    BU_CDP_WS=<wss://…>
    BU_BROWSER_LIVE_URL=<https://live.browser-use.com/...>
    BU_BROWSER_EXPIRES_AT=<unix-epoch>

Rotation: BU sessions cap at 240 min. We request a 239-min session, rotate
30 min before expiry, keep the previous browser alive for 60 s grace so
any in-flight task picks up the new env on its next call.

One keeper per box = one browser per box. The browser is bound to the
box's single profile_id. Privacy: no sharing across users or across boxes.
"""

import json
import os
import pathlib
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get('BROWSER_USE_API_BASE', 'https://api.browser-use.com/api/v3')
API_KEY = os.environ.get('BROWSER_USE_API_KEY') or sys.exit('BROWSER_USE_API_KEY not set')
PROFILE_ID = os.environ.get('BUX_PROFILE_ID') or sys.exit('BUX_PROFILE_ID not set')

STATE_DIR = pathlib.Path('/home/bux/.claude')
ENV_FILE = STATE_DIR / 'browser.env'
STATE_DIR.mkdir(parents=True, exist_ok=True)

SESSION_MIN = 239
ROTATE_AT_MIN_LEFT = 30
GRACE_SEC = 60


def bu_req(method, path, body=None, timeout=30):
	data = json.dumps(body).encode() if body is not None else None
	req = urllib.request.Request(
		f'{API}{path}',
		data=data,
		headers={'X-Browser-Use-API-Key': API_KEY, 'Content-Type': 'application/json'},
		method=method,
	)
	with urllib.request.urlopen(req, timeout=timeout) as r:
		raw = r.read()
		return json.loads(raw) if raw else {}


def log(msg):
	print(f'[keeper] {msg}', flush=True)


def cdp_ws_from_url(cdp_url):
	try:
		with urllib.request.urlopen(f'{cdp_url}/json/version', timeout=15) as r:
			return json.loads(r.read()).get('webSocketDebuggerUrl', '')
	except Exception as e:
		log(f'cdp version fetch failed: {e}')
		return ''


def create_browser(profile_id):
	r = bu_req('POST', '/browsers', {'profile_id': profile_id, 'timeout': SESSION_MIN})
	bid = r.get('id')
	cdp_url = r.get('cdpUrl') or r.get('cdp_url')
	ws = cdp_ws_from_url(cdp_url) if cdp_url else ''
	# liveUrl points at the public live-view UI (https://live.browser-use.com/...).
	# Same value GET /browsers/{id} returns; cached here so consumers don't need
	# an extra API call.
	live_url = r.get('liveUrl') or r.get('live_url') or ''
	expires_at = int(time.time() + SESSION_MIN * 60)
	return bid, ws, live_url, expires_at


def stop_browser(bid):
	try:
		bu_req('PATCH', f'/browsers/{bid}', {'action': 'stop'})
	except Exception as e:
		log(f'stop {bid} failed: {e!r}')


def health_check(ws):
	if not ws:
		return False
	try:
		http = ws.replace('wss://', 'https://').split('/devtools/')[0]
		with urllib.request.urlopen(f'{http}/json/version', timeout=10) as r:
			r.read()
		return True
	except Exception as e:
		log(f'health check failed: {e!r}')
		return False


def write_env(profile_id, bid, ws, live_url, expires_at):
	# Contains the signed CDP URL (BU_CDP_WS) — world-readable window is
	# unacceptable. O_EXCL guarantees the file is freshly created (we own
	# the mode); O_CLOEXEC prevents accidental fd inheritance. Any stale
	# .tmp from a prior crash is unlinked first so O_EXCL doesn't fail.
	tmp = ENV_FILE.with_suffix('.tmp')
	payload = (
		f'BU_PROFILE_ID={profile_id}\n'
		f'BU_BROWSER_ID={bid}\n'
		f'BU_CDP_WS={ws}\n'
		f'BU_BROWSER_LIVE_URL={live_url}\n'
		f'BU_BROWSER_EXPIRES_AT={expires_at}\n'
	)
	try:
		os.unlink(tmp)
	except FileNotFoundError:
		pass
	fd = os.open(
		str(tmp),
		os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_CLOEXEC,
		0o640,
	)
	try:
		os.write(fd, payload.encode())
	finally:
		os.close(fd)
	tmp.replace(ENV_FILE)  # atomic rename preserves the tempfile's mode
	# Ensure bux owns it (keeper usually runs as bux, but belt-and-suspenders).
	try:
		import pwd

		u = pwd.getpwnam('bux')
		os.chown(str(ENV_FILE), u.pw_uid, u.pw_gid)
	except Exception:
		pass


_current = None


def shutdown(*_):
	if _current:
		log(f'shutting down, stopping {_current[0]}')
		stop_browser(_current[0])
	sys.exit(0)


def main():
	global _current
	signal.signal(signal.SIGTERM, shutdown)
	signal.signal(signal.SIGINT, shutdown)

	log(f'bound to profile {PROFILE_ID}')

	while True:
		try:
			bid, ws, live_url, expires_at = create_browser(PROFILE_ID)
			log(f'created browser {bid} ws={ws[:60]}… expires_at={expires_at}')

			if not health_check(ws):
				log('fresh browser failed health check, retrying in 15s')
				stop_browser(bid)
				time.sleep(15)
				continue

			prev = _current
			_current = (bid, ws, expires_at)
			write_env(PROFILE_ID, bid, ws, live_url, expires_at)
			log(f'wrote {ENV_FILE}')

			if prev:
				time.sleep(GRACE_SEC)
				log(f'stopping previous browser {prev[0]} after {GRACE_SEC}s grace')
				stop_browser(prev[0])

			sleep_until = expires_at - ROTATE_AT_MIN_LEFT * 60
			sleep_for = max(60, sleep_until - time.time())
			log(f'sleeping {int(sleep_for)}s before rotation')
			time.sleep(sleep_for)
		except Exception as e:
			log(f'loop error: {e!r}, sleeping 30s')
			time.sleep(30)


if __name__ == '__main__':
	main()
