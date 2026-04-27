# Your environment (this box)

You are running on **bux** — a persistent Linux box with Claude Code, a browser-harness skill, and a long-lived Chromium session via Browser Use Cloud. There is **no local Chrome/Chromium/Playwright** on this host. Always drive through the pre-configured Browser Use Cloud session.

## How to use the browser

A long-lived Browser Use Cloud browser session is already running, bound to this box's profile. Connection details are in `~/.claude/browser.env`:

```
BU_PROFILE_ID=<uuid>
BU_BROWSER_ID=<id>
BU_CDP_WS=wss://connect.browser-use.com/...
BU_BROWSER_LIVE_URL=https://live.browser-use.com/...
BU_BROWSER_EXPIRES_AT=<unix epoch>
```

These refresh automatically — the `bux-browser-keeper` service rotates sessions before they expire. You don't have to manage the session lifecycle.

### Driving the browser

The **browser-harness** skill is installed at `~/.claude/skills/cdp/`. It gives you direct typed CDP access:

```bash
source ~/.claude/browser.env
browser-harness-js "await session.connect({wsUrl: process.env.BU_CDP_WS})"
browser-harness-js "await session.Page.navigate({url: 'https://example.com'})"
browser-harness-js "await session.Runtime.evaluate({expression: 'document.title'})"
```

Or in one line:

```bash
source ~/.claude/browser.env && browser-harness-js 'await session.connect({wsUrl: process.env.BU_CDP_WS}); await session.Page.navigate({url: "https://example.com"})'
```

`browser-harness-js` is a Bun-based CLI that keeps a persistent Session object alive between calls — every invocation shares the same connection. See `~/.claude/skills/cdp/SKILL.md` for the full API (652 typed CDP methods).

### The browser has the user's logins (over time)

Cookies + localStorage persist via the bound profile. A **fresh/empty profile** starts with no logins — the user will need to log in once per site, and the profile remembers it after that. If the profile was seeded from an existing logged-in browser, those logins are already in place.

### When you hit a login wall, 2FA, CAPTCHA, or otherwise can't continue

**Stop. Don't guess, don't credential-stuff, don't give up.** Hand the browser to the user via the live view URL and wait.

1. Read the live URL straight out of `~/.claude/browser.env` (the keeper writes it on every rotation):

   ```bash
   source ~/.claude/browser.env
   echo "$BU_BROWSER_LIVE_URL"
   ```

   (If for some reason that variable is empty, you can also fetch it from the API: `curl -sS -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY" "https://api.browser-use.com/api/v3/browsers/$BU_BROWSER_ID" | jq -r '.liveUrl'`.)

2. Tell the user exactly what's blocking you and what they need to do, then share the URL. Example:

   > I can't continue — LinkedIn needs you to sign in. Open this and complete the login, then tell me "done":
   > **https://live.browser-use.com?wss=...**

3. **Wait for the user to reply** before resuming. Don't poll, don't retry — they'll come back when it's their turn.

4. Once they say "done", continue from where you left off. The session cookies are now persisted in the profile; you won't have to ask again for that site.

This works for: login pages, SMS / email / authenticator 2FA, CAPTCHAs, cookie-consent dialogs that refuse to dismiss, session-expired re-auth, Cloudflare / anti-bot challenges — anything that needs a human touch. **Prefer handing off over trying to solve it yourself.** The user would rather click once and keep going than watch you burn 15 minutes fighting a login form.

### Live view (debugging / watch-along)

Share the live URL any time the user asks "what is the browser doing?" or when you want them to watch along for a tricky flow:

```bash
source ~/.claude/browser.env && echo "$BU_BROWSER_LIVE_URL"
```

### Switching to a different profile

The box is bound to one Browser Use Cloud profile at a time. If the user asks to switch ("use my work profile", "rebind to profile `<uuid>`", "start fresh with a new empty profile"), YOU can do it:

1. **List their profiles:**

   ```bash
   curl -sS -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY" \
     'https://api.browser-use.com/api/v3/profiles' | jq
   ```

2. **Swap `BUX_PROFILE_ID`** in `/etc/bux/env` (writable because the `bux` group owns `/etc/bux`):

   ```bash
   sudo sed -i "s|^BUX_PROFILE_ID=.*|BUX_PROFILE_ID=<new-uuid>|" /etc/bux/env
   ```

   (Or create a new profile first: `curl -X POST -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY" -H "Content-Type: application/json" -d '{"name":"<name>"}' https://api.browser-use.com/api/v3/profiles`)

3. **Restart the keeper** so it picks up the new profile:

   ```bash
   sudo systemctl restart bux-browser-keeper
   ```

4. Wait ~10s, then `source ~/.claude/browser.env` in a fresh shell — `BU_PROFILE_ID` and `BU_BROWSER_ID` will be the new values.

Only do this when the user explicitly asks. Don't silently rebind across tasks.

## Scheduling and reminders

When the user asks you to "remind me in 5 minutes", "schedule X for 9am tomorrow", "every weekday at 8am do Y" etc., **use local `at` + cron + the `tg-send` helper**. Do NOT use Claude Code's `/routines` or in-session schedulers — those die the moment your `claude -p` session exits (which happens within seconds on this box) and the user never gets pinged.

### `tg-send` — push a Telegram message from any shell

`tg-send` posts a message to the user's bound TG chat. It accepts the message either as an argument **or** on stdin, so it pipes naturally:

```bash
tg-send "Reminder: check your Slack"            # arg form
echo "all done" | tg-send                       # stdin form
claude -p "summarize my email" | tg-send        # the recurring use case
```

- Reads the bot token from `/etc/bux/tg.env` (mode 640 root:bux, readable by you).
- Reads the bound chat id from `/etc/bux/tg-allowed.txt`.
- Plain text only — the bot's own handler does MarkdownV2 rendering, so don't try to send markup via this path.
- Output > 4 KB is truncated with `…(truncated)` so a long claude reply doesn't 400.

### One-shot reminders (`at`)

```bash
echo 'tg-send "Reminder: check your Slack"' | at now + 5 minutes
```

`at` runs the body as a shell script when the timer fires, so the body needs to *call* tg-send (not be piped *to* it). To list pending: `atq`. To cancel: `atrm <jobid>`.

For things that need claude itself to do work at fire time, wrap a `claude -p` call and pipe its output:

```bash
echo 'claude -p "summarize my unread email" | tg-send' | at 9am
```

(The outer `echo … | at …` is what schedules the job. Inside the job, `claude -p` produces output that gets piped to `tg-send`.)

### Recurring schedules (`cron`)

Add to bux's crontab via `crontab -e`. Standard 5-field format. Always pipe to `tg-send` so the user actually sees the result.

```cron
# Every weekday at 8 UTC, summarize unread email and ping the user
0 8 * * 1-5  claude -p "summarize my unread email in 5 bullets" | tg-send
```

Avoid spamming — daily reminders are usually fine, sub-hourly probably isn't unless the user explicitly asked.

### When the user "schedules" a task in TG

1. Pick the right tool: `at` for one-shot, `cron` for recurring.
2. Wrap the work so it ends with `tg-send "<result>"`. The user must hear back.
3. Confirm **what** and **when** (in UTC) so they can tell if you misparsed "5pm Pacific".

## Conventions on this box

- **Working directory**: default is `/home/bux`. Keep task artifacts here.
- **Shared notebook**: `/home/bux/notebook.md` is a scratch file for cross-task continuity. Read it at the start of a task, append useful findings at the end.
- **Prefer browser-harness over calling HTTP APIs directly** when the user asks about a website. Sessions persist logins; HTTP calls don't.
- **Keep the box tidy**: avoid installing global npm / apt packages unless necessary. A small, boring box is easier to reason about.

## Don't do

- Don't run `playwright install`, `apt install chromium`, `brew install chrome`, etc. The box has no Chrome and never will.
- Don't assume `BROWSER_USE_API_KEY` or any BU env is in your shell — always `source ~/.claude/browser.env` first.
- Don't try to log in to sites on behalf of the user unless they explicitly give you credentials. Say so clearly and ask.
- Don't use Claude Code routines / `/routines` URLs for time-deferred work. They fire in claude.ai's runtime, which has no path back to this box. Use `at` + `tg-send` instead.
