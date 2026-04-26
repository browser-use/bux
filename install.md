# Installing bux

bux runs on any box that runs Ubuntu / Debian 22.04+. 2GB RAM is enough. Pick whatever's cheapest — the cloud browser lives on browser-use infrastructure, not your box.

## The 30-second version

```bash
ssh root@your-box
curl -fsSL https://raw.githubusercontent.com/browser-use/bux/main/install.sh \
  | sudo BROWSER_USE_API_KEY=bu_xxx bash
```

That's it. Skip to [first run](#first-run).

---

## Step by step

### 1. Get a box

Any of these work. Pick one you already use.

**VPS providers** (5 min, most portable)
- **Hetzner** — cheapest; CX11 (€4/mo, 2 vCPU / 2GB) is plenty. Falkenstein or Nuremberg for EU latency, Ashburn for US.
- **DigitalOcean** — $6/mo droplet, ubuntu 24.04 image.
- **Fly.io / Railway** — sized too small by default; bump to 2GB.
- **AWS EC2** — `t3.small` minimum. If you want auto-provisioning with one command, the [Browser Use Cloud managed version](https://cloud.browser-use.com) handles AMI baking + launch for you.

**Home lab** (0 min, no recurring cost)
- **Mac mini** with Ubuntu Asahi, or a Raspberry Pi 4/5. Expose via Tailscale (recommended) or Cloudflare Tunnel. No open ports to the internet needed.

**Existing server**
- If you already have a dev box, bux can share it — it adds a `bux` user and runs everything under `/opt/bux`. Installer is idempotent.

### 2. Get API keys

**Browser Use Cloud** — https://cloud.browser-use.com/new-api-key (free tier: 3 concurrent browsers, proxies, CAPTCHA).

**Telegram bot (optional)** — message [@BotFather](https://t.me/BotFather) on Telegram:

```
/newbot
<pick any name, e.g. "my-agent">
<pick any username ending in _bot, e.g. my_agent_bot>
```

BotFather replies with a token like `1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`. Save it.

### 3. Run the installer

```bash
ssh root@your-box

# Interactive (it'll ask for anything missing):
curl -fsSL https://raw.githubusercontent.com/browser-use/bux/main/install.sh | sudo bash

# Or one-shot with everything up front:
curl -fsSL https://raw.githubusercontent.com/browser-use/bux/main/install.sh \
  | sudo BROWSER_USE_API_KEY=bu_xxx TG_BOT_TOKEN=123:abc bash
```

The script:
1. Installs Node.js 24 + Claude Code + ttyd + browser-harness
2. Creates a `bux` system user with its own venv
3. Drops the browser-keeper + telegram-bot + systemd units
4. Starts everything

Takes ~2-3 minutes on a fresh box. Idempotent — safe to rerun after edits.

### 4. First run

```bash
sudo -iu bux        # become the bux user
cd ~ && claude
```

On first launch claude will ask you to log in. Type `/login` and complete the OAuth flow in your laptop browser. Once authed, exit claude (`/exit`). From now on `claude` starts straight into a session.

### 5. Bind the Telegram bot

If you passed `TG_BOT_TOKEN`, the installer printed a `t.me/<bot>` URL. Open it on your phone, send any message ("hi" works). **The first chat wins** — after you bind, the bot ignores everyone else forever.

Try it:

```
you: hi
bot: 🔒 This bot is now locked to this chat only.

you: /live
bot: 🖥 https://live.browser-use.com?wss=...

you: check my email, find unread from today, one-line summary each
bot: 🧠 on it…
bot: 3 unread from today:
     • Stripe: invoice for April usage ready
     • …
```

### 6. You're done

Every message is its own claude turn but **shares memory** with the previous ones. Follow-ups work:

```
you: check my email
bot: [summary]
you: reply to the stripe one saying pay it next week
bot: done, want to see the draft first?
```

## Troubleshooting

**`browser.env` isn't created / browser-keeper crashes**
```bash
sudo journalctl -u bux-browser-keeper -n 50
```
Most common cause: bad `BROWSER_USE_API_KEY`. Edit `/etc/bux/env`, restart.

**TG bot silent after sending a message**
```bash
sudo journalctl -u bux-tg -n 50
```
- `dropping msg from chat_id=... (already bound)` → someone else's chat_id is bound. Wipe `/etc/bux/tg.env`, rerun install, bind again.
- `invalid bot token` → regenerate via @BotFather.

**claude errors on `--session-id` or `--resume`**
You have an older Claude Code version. Update:
```bash
sudo npm install -g @anthropic-ai/claude-code@latest
sudo systemctl restart bux-tg
```

**claude says "no CDP_WS set"**
The browser-keeper hasn't written `~/.claude/browser.env` yet. Wait 10s on first boot, or:
```bash
sudo systemctl restart bux-browser-keeper
cat /home/bux/.claude/browser.env   # should have BU_CDP_WS=wss://...
```

**Need a clean slate**
```bash
sudo systemctl stop bux-tg bux-browser-keeper bux-ttyd
sudo rm -rf /etc/bux /opt/bux /home/bux/.claude /home/bux/.bux
sudo userdel -r bux
# rerun install.sh
```

## What's next

- [SKILL.md](SKILL.md) — how claude uses the browser (auto-loaded as `CLAUDE.md` context)
- [browser-harness](https://github.com/browser-use/browser-harness) — the CDP skill powering the browser
- [docs/recipes/](docs/recipes/) — provider-specific deploy notes
