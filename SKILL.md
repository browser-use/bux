# How claude uses the box

This file is copied to `/home/bux/CLAUDE.md` by the installer — claude auto-loads it as context any time it runs in `/home/bux`. It tells the agent what's on the box, how to drive the browser, and when to hand off to the user.

Source: [agent/CLAUDE.md](agent/CLAUDE.md).

## If you want to customize behavior

Edit `agent/CLAUDE.md`, rerun `install.sh` (it's idempotent), and the next claude run picks up the changes. Things worth customizing:

- **Working directory layout** — `/home/bux` is the default. If you want task artifacts in a different tree, edit the "Conventions" section.
- **Skill policies** — "when you hit a login wall, prompt the user" is the out-of-the-box behavior. If you want silent auto-retry, or always-ask-before-acting, or per-domain behavior, tell claude here.
- **Allowed tools / permissions** — baseline is `--permission-mode bypassPermissions` inside systemd services. Tighten via `~/.claude.json` if needed.

## Teaching claude new things

The agent writes its own [browser-harness](https://github.com/browser-use/browser-harness) skills as it works — so you don't hand-author helpers for LinkedIn / Gmail / Amazon / etc. Just run your task and the agent builds up `~/.claude/skills/cdp/domain-skills/<site>/` over time. These persist across reboots because they live in `/home/bux`.
