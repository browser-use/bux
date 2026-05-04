#!/usr/bin/env bash
# bootstrap.sh — wire up bux on a fresh box (or after a `git pull` update).
#
# Runs as root. Idempotent: re-running is safe and re-asserts every unit /
# polkit rule / login hook to whatever this commit's defaults are. The
# AMI-baked dependencies (python venv, node, claude CLI, ttyd) are NOT
# installed here — that's the AMI's job. This script only handles the
# parts that change with the agent code.
#
# Used in two places:
#   1. First boot: cloud user-data clones this repo to /opt/bux/agent
#      and runs `bash /opt/bux/agent/bootstrap.sh`.
#   2. Update: agent's `update` cmd runs `git pull` then re-runs
#      bootstrap.sh so any new systemd unit / polkit change lands.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_DIR="$REPO_DIR/agent"
VENV="${VENV:-/opt/bux/venv}"

if [ "$(id -u)" -ne 0 ]; then
  echo "bootstrap.sh must run as root" >&2
  exit 1
fi

# --- log dir (used by every systemd unit's StandardOutput=append:...) ------
install -d -o bux -g bux -m 0755 /var/log/bux

# --- python deps ----------------------------------------------------------
# /opt/bux/venv is baked into the AMI with a wide set of pre-installs (see
# packer/install.sh). On update, only run pip install if requirements.txt
# changed since last boot — this is "fast path" updates.
if [ -f "$AGENT_DIR/requirements.txt" ]; then
  REQ_HASH_FILE=/var/lib/bux/requirements.hash
  install -d -o root -g root -m 0755 /var/lib/bux
  NEW_HASH=$(sha256sum "$AGENT_DIR/requirements.txt" | awk '{print $1}')
  OLD_HASH=$(cat "$REQ_HASH_FILE" 2>/dev/null || echo "")
  if [ "$NEW_HASH" != "$OLD_HASH" ]; then
    echo "bootstrap: requirements.txt changed; pip installing"
    sudo -u bux "$VENV/bin/pip" install --quiet -r "$AGENT_DIR/requirements.txt"
    echo "$NEW_HASH" > "$REQ_HASH_FILE"
  fi
fi

# --- browser-harness refresh ---------------------------------------------
# browser-harness changes often (separate repo, separate cadence). Treat it
# the same way we treat agent code: pull the upstream, reinstall via uv
# only when the SHA actually moved. Keeps `/update` cheap when nothing's
# changed and lets harness fixes ship without an AMI rebake.
#
# AMI-baked first boot: the clone already exists at /home/bux/src/browser-
# harness from packer/install.sh, so this just confirms it's current.
HARNESS_DIR=/home/bux/src/browser-harness
if [ -d "$HARNESS_DIR/.git" ]; then
  HARNESS_HASH_FILE=/var/lib/bux/harness.sha
  install -d -o root -g root -m 0755 /var/lib/bux
  # ff-only so a force-pushed harness doesn't silently rewrite local
  # history on the box; user can always manually reset if intentional.
  sudo -u bux git -C "$HARNESS_DIR" fetch --quiet --depth=1 origin || true
  sudo -u bux git -C "$HARNESS_DIR" reset --quiet --hard origin/HEAD || true
  NEW_HARNESS_SHA=$(sudo -u bux git -C "$HARNESS_DIR" rev-parse HEAD)
  OLD_HARNESS_SHA=$(cat "$HARNESS_HASH_FILE" 2>/dev/null || echo "")
  if [ "$NEW_HARNESS_SHA" != "$OLD_HARNESS_SHA" ]; then
    echo "bootstrap: browser-harness sha changed ($OLD_HARNESS_SHA → $NEW_HARNESS_SHA); reinstalling"
    # uv tool install --force re-pins the entrypoint at /home/bux/.local/
    # bin/browser-harness against the new tree. Run as bux (-H so HOME
    # resolves) since the install lands under /home/bux/.local.
    sudo -u bux -H "$(command -v uv)" tool install --force \
      --from "$HARNESS_DIR" browser-harness
    echo "$NEW_HARNESS_SHA" > "$HARNESS_HASH_FILE"
  fi
fi

# --- Codex CLI (alternative agent, /codex per forum topic) ----------------
# install.sh installs codex on first boot, but boxes provisioned before
# that block existed (or where the npm install hit a transient failure
# and got skipped as non-fatal) end up without it — the user discovers
# this when `/codex` reports "codex is not installed". Re-check on every
# update so the install self-heals. Idempotent: skipped when codex is
# already on bux's PATH. Runs as bux so the binary lands under
# /home/bux/.npm-global/bin (already on bux's PATH via .profile).
if command -v npm >/dev/null 2>&1 && ! sudo -iu bux command -v codex >/dev/null 2>&1; then
  echo "bootstrap: installing Codex CLI for bux"
  sudo -iu bux npm install -g @openai/codex \
    || echo "bootstrap: codex install failed (non-fatal — /codex login will hint how to install later)" >&2
fi

# --- agent shell helpers --------------------------------------------------
# install.sh creates these symlinks on first boot, but new helpers added to
# agent/ after a box has already been provisioned never get linked into
# /usr/local/bin without a re-bootstrap. Re-assert here on every update so
# the symlinks track agent/ as new helpers ship. Idempotent (ln -sfn).
ln -sfn "$REPO_DIR/agent/tg-send"        /usr/local/bin/tg-send
ln -sfn "$REPO_DIR/agent/tg-buttons"     /usr/local/bin/tg-buttons
ln -sfn "$REPO_DIR/agent/agency-report"  /usr/local/bin/agency-report
ln -sfn "$REPO_DIR/agent/bux-restart"    /usr/local/bin/bux-restart

# Agency DB lives at /var/lib/bux/agency.db (created by agency_db on
# first use). Make sure the directory is writable by `bux` so any
# agency-report invocation can init the schema without sudo.
install -d -o bux -g bux -m 0755 /var/lib/bux

# --- Cloud Composio MCP server (cloud-side proxy) -------------------------
# Why MCP at all: cloud holds the platform's Composio API key plus every
# integration the user OAuth'd via cloud.browser-use.com. Rather than
# duplicating that ceremony on each box (Composio key, per-toolkit auth
# configs, OAuth callbacks, refresh-token storage), we point Claude Code
# at a cloud-hosted MCP endpoint that proxies tool calls through with the
# box's project_id as the Composio entity_id. Net effect: any toolkit the
# user has connected on cloud (Gmail, Calendar, Slack, …) is automatically
# available to the box agent as native tools — zero per-box setup.
#
# Token rotation: BUX_BOX_TOKEN gets baked into ~/.claude.json by
# `claude mcp add` at registration time. If the cloud rotates the token,
# the next /update re-runs this section, which removes + re-adds the MCP
# server with the fresh token. Manual rotation: re-run bootstrap.sh.
#
# To disable: as the bux user, `claude mcp remove composio`. The next
# /update will re-add it unless this section is removed too.
if [ -f /etc/bux/env ]; then
  # shellcheck disable=SC1091
  . /etc/bux/env || true
fi
if [ -z "${BUX_BOX_TOKEN:-}" ]; then
  echo "bootstrap: BUX_BOX_TOKEN not set; skipping cloud Composio MCP registration" >&2
elif ! command -v claude >/dev/null 2>&1; then
  echo "bootstrap: claude CLI not on PATH; skipping cloud Composio MCP registration" >&2
else
  # Idempotent: remove any prior entry (ignore failure if it didn't exist),
  # then re-add against the current token. -H so HOME resolves to /home/bux
  # and the registration lands in bux's ~/.claude.json, not root's.
  #
  # `--scope user` is critical: without it, `claude mcp add` defaults to
  # `--scope local`, which writes the MCP entry under the *current working
  # directory's* project record in ~/.claude.json. bootstrap.sh's CWD
  # depends on who invoked it (cloud-init runs us from /, packer from
  # /opt/bux/repo, /update from /home/bux), so the MCP would land in a
  # random project entry the bot's claude session never visits. The bot
  # always runs claude from /home/bux (see CLAUDE.md), so a registration
  # under e.g. `projects./opt/bux/repo` is dead weight — `claude mcp list`
  # from /home/bux returns nothing for composio. User-scope is project-
  # independent and matches the "available to every claude session as bux"
  # intent. The `claude mcp remove` call above doesn't take --scope, so it
  # finds and clears the entry regardless of which scope it was previously
  # written to (handles the cleanup of the old buggy local-scope entries).
  sudo -u bux -H claude mcp remove composio >/dev/null 2>&1 || true
  # Subshell with `set +x` so the bearer token never lands in trace output
  # (currently bootstrap is set -euo pipefail without -x, but if anyone
  # turns on tracing for debugging they shouldn't accidentally leak the
  # token to /var/log/bux/install.log or the user-data console log).
  ( set +x; sudo -u bux -H claude mcp add --scope user --transport http composio \
    https://api.browser-use.com/cloud/composio/mcp \
    --header "Authorization: Bearer $BUX_BOX_TOKEN" >/dev/null ) || \
    echo "bootstrap: WARN failed to register cloud Composio MCP server; continuing bootstrap" >&2
  # Verify the registration actually wrote a usable entry. A silent
  # failure here means the user doesn't get cloud integrations until
  # their next /update — this fail-loud check turns that into a
  # bootstrap-time error we'll see in install.log instead. Run `mcp list`
  # from /home/bux specifically because that's the directory the bot's
  # claude session runs from — if the registration doesn't surface here,
  # the bot won't see it, so the verification has to match the consumer.
  if ! sudo -u bux -H bash -c 'cd /home/bux && claude mcp list 2>/dev/null' | grep -q '^composio'; then
    echo "bootstrap: WARN composio MCP registration didn't take" >&2
  else
    echo "bootstrap: registered cloud Composio MCP server"
  fi
fi

# --- login banner: live browser URL on each ssh login ---------------------
if ! grep -q 'BU_BROWSER_LIVE_URL' /home/bux/.profile 2>/dev/null; then
  cat >> /home/bux/.profile <<'PROFILE'

# Show the live browser URL so users have one click to spectate / take over.
if [ -r "$HOME/.claude/browser.env" ]; then
  . "$HOME/.claude/browser.env" 2>/dev/null || true
  if [ -n "${BU_BROWSER_LIVE_URL:-}" ]; then
    printf '\n  \033[1mLive browser:\033[0m %s\n\n' "$BU_BROWSER_LIVE_URL"
  fi
fi
PROFILE
  chown bux:bux /home/bux/.profile
fi

# --- polkit: let bux user manage bux-tg.service via systemctl --------------
# The agent (running as bux) shells out `systemctl restart bux-tg` after
# writing /etc/bux/tg.env. Without this rule, polkit would require an
# interactive prompt or sudo.
# --- git safe.directory so root tools can read the bux-owned repo --------
# /opt/bux/repo is owned by bux. When telegram_bot.py (User=root) shells
# out to git for /version or /update, git rejects with "dubious ownership"
# unless we trust the dir. System-wide config is the cleanest fix.
git config --system --add safe.directory /opt/bux/repo

# --- sudoers: let bux re-run bootstrap.sh during self-update --------------
# box-agent runs as bux. The `update` cmd handler does git pull + bash
# bootstrap.sh; bootstrap.sh writes /etc/systemd/* and /etc/cron.d/*, which
# require root. Grant a narrow sudoers rule for exactly this script (any
# checkout under the bux-owned repo dir).
cat > /etc/sudoers.d/bux-bootstrap <<'SUDOERS'
bux ALL=(root) NOPASSWD: /opt/bux/repo/agent/bootstrap.sh
bux ALL=(root) NOPASSWD: /bin/bash /opt/bux/repo/agent/bootstrap.sh
SUDOERS
chmod 440 /etc/sudoers.d/bux-bootstrap

cat > /etc/polkit-1/rules.d/50-bux-chat.rules <<'POLKIT'
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        subject.user == "bux") {
        var unit = action.lookup("unit");
        // bux-tg: agent restarts after writing /etc/bux/tg.env on install.
        // box-agent: agent restarts itself at the tail of self-update so
        //   the new code takes effect.
        // bux-browser-keeper / bux-ttyd: same self-update path.
        if (unit == "bux-tg.service" ||
            unit == "box-agent.service" ||
            unit == "bux-browser-keeper.service" ||
            unit == "bux-ttyd.service") {
            return polkit.Result.YES;
        }
    }
});
POLKIT
chmod 644 /etc/polkit-1/rules.d/50-bux-chat.rules

# --- systemd units --------------------------------------------------------
# Symlink rather than copy so a `git pull` propagates without re-running
# bootstrap. systemd reads via the symlink fine.
for unit in box-agent.service bux-ttyd.service bux-browser-keeper.service bux-tg.service; do
  ln -sf "$AGENT_DIR/$unit" "/etc/systemd/system/$unit"
done

# --- boot-time pull oneshot ------------------------------------------------
# On every reboot, pull latest agent code from OSS and re-run bootstrap.sh
# BEFORE the long-lived units start. Same idea as the user-data first-boot
# pull on the cloud side, but covers the case of an existing box getting
# rebooted (stop+start, instance refresh, etc.) — without this, a user-
# triggered reboot could revert the box to whatever it had on disk last,
# missing fixes that landed in OSS while it was running.
#
# Type=oneshot + Before=box-agent.service so the pull always lands before
# the agent starts. Best-effort: a github outage at boot logs a warning
# but doesn't block the agent from coming up on the previous SHA.
cat > /etc/systemd/system/bux-boot-update.service <<'UNITEOF'
[Unit]
Description=bux boot-time git pull + bootstrap
After=network-online.target
Wants=network-online.target
Before=box-agent.service bux-tg.service bux-browser-keeper.service bux-ttyd.service

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'sudo -u bux git -C /opt/bux/repo pull --ff-only --quiet || true; /bin/bash /opt/bux/agent/bootstrap.sh'
StandardOutput=append:/var/log/bux/boot-update.log
StandardError=append:/var/log/bux/boot-update.log
# A long fetch shouldn't block boot indefinitely. 60s is enough for a
# shallow pull on a healthy network; on timeout we skip and the agent
# starts on the existing on-disk code.
TimeoutStartSec=60
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
UNITEOF

# Drop any unit from a previous version that no longer exists in this
# commit (e.g. bux-slack.service after Slack removal). Keeps systemd's
# unit registry in sync with the repo.
for stale in bux-slack.service; do
  if [ -e "/etc/systemd/system/$stale" ] && [ ! -e "$AGENT_DIR/$stale" ]; then
    systemctl disable --now "$stale" 2>/dev/null || true
    rm -f "/etc/systemd/system/$stale"
  fi
done

systemctl daemon-reload

# Always-on units. They'll start when their ConditionPathExists files
# (/etc/bux/env etc.) are present.
systemctl enable box-agent.service
systemctl enable bux-ttyd.service
systemctl enable bux-browser-keeper.service

# bux-tg stays enabled-but-conditional — only runs once /etc/bux/tg.env
# is written by the agent's tg_install handler.
systemctl enable bux-tg.service

# Boot-time pull runs ahead of the others on every reboot.
systemctl enable bux-boot-update.service

# --- self-heal cron -------------------------------------------------------
# A user with sudo can `systemctl disable box-agent`, leaving the box
# unmanageable from the cloud. This cron re-enables the agent every 5 min
# regardless of user state. They can still kill it for a few minutes; they
# can't permanently disable it.
cat > /etc/cron.d/bux-self-heal <<'CRON'
# Re-enable box-agent if disabled (user-tampering guard).
*/5 * * * * root /bin/systemctl is-enabled box-agent.service >/dev/null 2>&1 || /bin/systemctl enable --now box-agent.service
*/5 * * * * root /bin/systemctl is-active box-agent.service >/dev/null 2>&1 || /bin/systemctl restart box-agent.service
CRON
chmod 644 /etc/cron.d/bux-self-heal

# --- restart services so the new code takes effect on update --------------
# On first boot the units start fresh from systemctl enable below; this
# restart is a no-op then. On update it picks up the new agent code.
systemctl restart box-agent.service 2>/dev/null || true
# bux-tg only restarts if it was already running (not started on first boot).
if systemctl is-active --quiet bux-tg.service; then
  systemctl restart bux-tg.service
fi
if systemctl is-active --quiet bux-browser-keeper.service; then
  systemctl restart bux-browser-keeper.service
fi
if systemctl is-active --quiet bux-ttyd.service; then
  systemctl restart bux-ttyd.service
fi

echo "bootstrap: done"
