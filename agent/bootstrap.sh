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

# --- bux-connect symlink (idempotent re-assert on /update) ----------------
# install.sh creates this on first install; bootstrap.sh re-asserts so
# existing boxes pick it up on the next /update without re-running install.
# The .secrets dir holds /home/bux/.secrets/composio.env which bux-connect
# reads/writes (mode 600).
install -d -o bux -g bux -m 0700 /home/bux/.secrets
ln -sf /opt/bux/agent/bux_connect.py /usr/local/bin/bux-connect

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
