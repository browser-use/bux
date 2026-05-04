"""Refill the swipe deck up to N pending suggestions per user.

Strategy: shell out to `claude -p` with a tight prompt that asks the /agency
skill to return a JSON array. Claude is invoked in non-streaming mode and we
strip ```json ``` fences before parsing. If parse fails, we fall back to
inserting the raw text as a single suggestion so the user at least sees
something to swipe rather than an empty deck.

A file lock at /tmp/bux-agency-seed.lock is held for the duration so two
concurrent API calls don't pile up parallel seeders.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Iterable

from . import db

LOG = logging.getLogger("agency.seeder")

LOCK_PATH = Path("/tmp/bux-agency-seed.lock")
TARGET_PENDING = 10
SEED_TIMEOUT_S = 600  # 10 min — agency runs are slow

PROMPT_TEMPLATE = """Use the /agency skill to surface {n} concrete next actions for me.

Return ONLY a JSON array (no prose, no fences, no commentary). Each element:
{{
  "title": "<10-word swipe headline>",
  "description": "<1-2 sentence why-it-matters>",
  "draft_action": "<paste-ready instruction the agent can execute on right-swipe>"
}}

Constraints:
- Items must be reversibly executable by an autonomous agent on right-swipe
  (drafts saved, messages sent, PRs merged, etc.). Avoid open-ended research.
- No duplicates — if you've surfaced something similar in prior runs (check
  /home/bux/notebook.md if useful), skip it.
- Lead with high-leverage items. The user only sees 10 cards at a time.
"""


def _claude_seed_cmd(prompt: str) -> list[str]:
    return [
        "/usr/bin/claude",
        "--dangerously-skip-permissions",
        "-p",
        prompt,
    ]


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _extract_json_array(text: str) -> list[dict] | None:
    """Best-effort extract a JSON array from an LLM reply that might wrap it.

    Tries: raw parse → fenced block → first '[' through matching ']'.
    """
    text = text.strip()
    for candidate in _candidates(text):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    return None


def _candidates(text: str) -> Iterable[str]:
    yield text
    m = _JSON_FENCE_RE.search(text)
    if m:
        yield m.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        yield text[start : end + 1]


def _run_seeder_subprocess(user_id: str, needed: int) -> int:
    prompt = PROMPT_TEMPLATE.format(n=needed)
    cmd = _claude_seed_cmd(prompt)
    LOG.info("seeder: running claude for user_id=%s needed=%d", user_id, needed)
    env = os.environ.copy()
    env.setdefault("HOME", "/home/bux")
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=SEED_TIMEOUT_S,
            env=env,
            cwd="/home/bux",
        )
    except subprocess.TimeoutExpired:
        LOG.warning("seeder: claude timed out after %ds", SEED_TIMEOUT_S)
        return 0
    if proc.returncode != 0:
        LOG.warning("seeder: claude exit=%d stderr=%s", proc.returncode, proc.stderr[:300])
    out = proc.stdout or ""
    items = _extract_json_array(out)
    if items is None:
        if not out.strip():
            return 0
        snippet = out.strip().splitlines()[0][:80]
        db.insert_suggestion(
            user_id=user_id,
            title=snippet or "Agency reply",
            description=out.strip()[:1000],
            draft_action="",
            source="agency-skill-raw",
        )
        return 1
    inserted = 0
    for item in items[:needed]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        db.insert_suggestion(
            user_id=user_id,
            title=title[:200],
            description=str(item.get("description") or "")[:2000],
            draft_action=str(item.get("draft_action") or "")[:5000],
            source="agency-skill",
        )
        inserted += 1
    return inserted


_seeder_threads: dict[str, threading.Thread] = {}
_seeder_lock = threading.Lock()


def maybe_refill(user_id: str) -> bool:
    """Kick a background refill if pending < TARGET_PENDING. Returns True if started."""
    pending = db.count_pending(user_id)
    if pending >= TARGET_PENDING:
        return False
    with _seeder_lock:
        existing = _seeder_threads.get(user_id)
        if existing and existing.is_alive():
            return False
        t = threading.Thread(
            target=_locked_refill,
            args=(user_id, TARGET_PENDING - pending),
            name=f"agency-seeder-{user_id}",
            daemon=True,
        )
        _seeder_threads[user_id] = t
        t.start()
    return True


def _locked_refill(user_id: str, needed: int) -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as fp:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("seeder: another process holds the lock; skipping")
            return
        try:
            n = _run_seeder_subprocess(user_id, max(needed, 1))
            LOG.info("seeder: inserted %d suggestion(s) for %s", n, user_id)
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def seed_demo(user_id: str = "demo") -> None:
    """Drop a handful of canned suggestions for browser-only smoke testing."""
    samples = [
        ("Reply to Sarah re: demo timing",
         "Sarah pinged in #wall-magnus 2h ago about the Tuesday demo slot. Draft saved.",
         "Send the saved Gmail draft to Sarah confirming the Tuesday slot."),
        ("Merge PR #4501 — Chrome harness fix",
         "Stale 1-line fix from 3 days ago, CI green, no review threads open.",
         "Merge browser-use/bux PR #4501 with squash+merge."),
        ("Grab burrito for dinner",
         "Past 18:00 PT, no calendar event, fridge tracker says empty. Chipotle on Castro is open.",
         "Order one carnitas burrito + chips from Chipotle Castro for pickup in 30m."),
        ("Close 6 stale Linear tickets",
         "All from Q3, last activity >60 days, marked won't-do in standup notes.",
         "Close Linear tickets ENG-1011 through ENG-1016 with comment 'cleanup, no longer in scope'."),
        ("Post Slack one-liner: cloud RUM is back up",
         "Datadog dashboard cleared at 14:02 PT, ~30m post-deploy.",
         "Post in #wall-magnus: 'cloud rum is back to baseline, was the new sampling cap — Magnus'."),
    ]
    for title, desc, action in samples:
        db.insert_suggestion(
            user_id=user_id,
            title=title,
            description=desc,
            draft_action=action,
            source="demo",
        )
