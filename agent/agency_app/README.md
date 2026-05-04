# bux agency mini app

A Telegram-WebApp-hosted, Tinder-style swipe deck of agency suggestions. The
agent surfaces 10 cards at a time; the user swipes:

- **right** — accept; the agent dispatches the suggestion's `draft_action`
  into the user's primary TG topic and runs it.
- **left** — dismiss; nothing happens beyond a row in the `decisions` log.
- **up** — open a feedback modal, type a revision; the agent files the note as
  a follow-up suggestion the next refill will use to re-propose.

Drained cards are auto-refilled from the [`/agency`](../../.claude/skills/agency/SKILL.md)
skill so the deck always has at least 10 pending items per user.

## Architecture

```
TG WebApp button ──→ https://<random>.trycloudflare.com
                       │
                       ▼
                 cloudflared quick tunnel
                       │
                       ▼
                http://127.0.0.1:8443      (uvicorn, User=bux)
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
  /api/suggestions  /api/swipe     /static/*
        │              │
        │              └─→ right-swipe → nohup bash -c 'claude -p ... | tg-send' &
        │                                  │
        ▼                                  ▼
   SQLite (/var/lib/bux/agency.db)   user's TG topic
```

## Auth

Every `/api/*` request (except `/api/health`) requires a valid Telegram
WebApp `initData` blob in the `Authorization: tma <initData>` header. The
server HMACs against `TG_BOT_TOKEN` per [Telegram's spec](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app),
then matches the decoded `user.id` against the box's owner allowlist
(`/etc/bux/tg-state.json` `box_owner` + every `owners[*].user_id`).

Demo mode: launch with `BUX_AGENCY_DEMO=1`, browse to `?demo=1`, and the
auth check is skipped (scoped to a synthetic `demo` user). For local dev
only.

## Local dev

```bash
cd /tmp/bux-agency-app/agent
BUX_AGENCY_DEMO=1 BUX_AGENCY_DB=/tmp/agency.db \
  /opt/bux/venv/bin/uvicorn agency_app.server:app --host 127.0.0.1 --port 8443 --reload
```

Then open `http://localhost:8443/?demo=1` in any browser. The startup hook
seeds five canned cards on a fresh demo DB.

## Deploy on a bux box

`/opt/bux/repo/agent/bootstrap.sh` (re-run via TG `/update`) installs
everything: `cloudflared`, the SQLite dir, three systemd units
(`bux-agency-app`, `bux-agency-tunnel`, `bux-agency-tunnel-url`), and the
polkit rule that lets `bux` restart them. The tunnel URL lands in
`/etc/bux/env` as `BUX_AGENCY_APP_URL`. The bot's `/swipe` command renders a
`web_app` inline button against that URL.

## Multi-user

Every `/api/*` call is scoped by the validated `user_id` from `initData`.
Deploying to a second bux box just needs that box's bot token + owner
user_id wired through `/etc/bux/tg.env` — the SQLite schema is keyed by
`user_id` so two users on the same box wouldn't see each other's cards
either (this isn't a security boundary, but a UX one — the box-level
allowlist is still the auth gate).

## V2 TODOs

- **Voice input** on swipe-up via the [Web Speech API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Speech_API) —
  start/stop button, render interim transcript, submit final.
- **Per-task forum topic**: when a right-swipe is dispatched, call
  `bot.createForumTopic(chat_id, name=suggestion.title)`, store the
  `topic_id` in the `tasks` table, and post status updates into the new
  topic instead of the user's primary topic.
- **Mid-execution permission asks**: when an in-progress task hits a
  reversible decision point (e.g. "checkout?", "post to X?"), the agent
  inserts a `source='checkout-ask'` suggestion that references the parent
  task. Right-swipe continues the task; left-swipe aborts. The active task
  pauses on a file lock until the new card lands.
- **Auto-refill cron**: `0 9 * * *` runs the seeder for any user whose deck
  hasn't been touched in >24h. Right now refills only fire when the user
  hits `/api/suggestions` with a deck under 10.
- **Named cloudflared tunnel** with a stable hostname so the TG button URL
  doesn't rotate on every tunnel restart. V1's quick tunnel re-randomizes;
  bootstrap re-captures the URL on every boot.
- **Up-swipe revision loop**: V1 just files the feedback as a new
  suggestion. V2 should mark the original as `revising`, kick a sub-agent
  with `{original, feedback}`, and re-queue the revised card with a
  `Revised` badge.
- **Swipe-card detail view**: tap-and-hold on a card to expand into the
  full `description` + any links, without losing the swipe gesture.
- **Backlog topic**: a single `Backlog` forum topic showing every running
  task, its status, and the per-task topic link.

## Files

- `server.py` — FastAPI app (3 endpoints + static)
- `db.py` — SQLite schema + helpers
- `auth.py` — Telegram WebApp initData validation
- `seeder.py` — `/agency` skill subprocess + JSON parse
- `dispatch.py` — right-swipe → backgrounded `claude -p | tg-send`
- `static/` — vanilla JS swipe UI (no build step)
- `capture_tunnel_url.sh` — scrapes `trycloudflare.com` URL into `/etc/bux/env`
