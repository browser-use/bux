"""Telegram WebApp initData validation + per-user authorization.

Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Validation steps:
  1. Parse initData (URL-encoded querystring).
  2. Pull `hash` aside; sort the remaining `key=value` pairs alphabetically by
     key, joined with '\n'. That's the data-check string.
  3. secret_key = HMAC_SHA256(key=b"WebAppData", message=bot_token)
  4. expected = hex(HMAC_SHA256(key=secret_key, message=data_check_string))
  5. Constant-time-compare expected vs the supplied hash.

After validation, decode the `user` JSON to get the Telegram user_id and
match it against the box's owner allowlist. Membership lives in two places:
  - TG_OWNER_ID env var (authoritative on a freshly-bound box)
  - /etc/bux/tg-state.json `box_owner.user_id` + per-chat `owners[*].user_id`
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.parse
from pathlib import Path

TG_ENV = Path(os.environ.get("BUX_TG_ENV", "/etc/bux/tg.env"))
TG_STATE = Path(os.environ.get("BUX_TG_STATE", "/etc/bux/tg-state.json"))


def _read_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def bot_token() -> str | None:
    tok = os.environ.get("TG_BOT_TOKEN")
    if tok:
        return tok
    return _read_kv(TG_ENV).get("TG_BOT_TOKEN")


def allowlisted_user_ids() -> set[str]:
    ids: set[str] = set()
    env_id = os.environ.get("TG_OWNER_ID", "").strip()
    if env_id:
        ids.add(env_id)
    try:
        state = json.loads(TG_STATE.read_text())
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        state = {}
    box_owner = (state.get("box_owner") or {}).get("user_id")
    if box_owner:
        ids.add(str(box_owner))
    for owner in (state.get("owners") or {}).values():
        uid = owner.get("user_id") if isinstance(owner, dict) else None
        if uid:
            ids.add(str(uid))
    return ids


def parse_init_data(init_data: str) -> dict[str, str]:
    """Parse initData querystring into a dict, preserving URL-decoded values."""
    return dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))


def verify_init_data(init_data: str, token: str) -> dict | None:
    """Return the decoded user payload if the signature is valid, else None.

    Constant-time compares the recomputed hash. Bumps `auth_date` outside the
    caller's purview — we don't enforce a freshness window here so a flaky
    network reload doesn't kick the user out mid-swipe; the bot allowlist is
    the real authorization gate.
    """
    if not init_data or not token:
        return None
    fields = parse_init_data(init_data)
    given_hash = fields.pop("hash", None)
    if not given_hash:
        return None
    data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given_hash):
        return None
    user_raw = fields.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    return user


def parse_authorization(header: str | None) -> str | None:
    """Extract the initData blob from `Authorization: tma <initData>`."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, value = parts
    if scheme.lower() != "tma":
        return None
    return value.strip()
