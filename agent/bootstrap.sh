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

# Enable Codex /goal autopilot feature: `[features] goals = true` in
# ~/.codex/config.toml. Idempotent — leaves existing config alone if
# goals=true or a [features] block is already present.
sudo -u bux -H bash -c '
CODEX_CONFIG="$HOME/.codex/config.toml"
mkdir -p "$(dirname "$CODEX_CONFIG")"
if [ ! -f "$CODEX_CONFIG" ]; then
  printf "[features]\ngoals = true\n" > "$CODEX_CONFIG"
elif ! grep -qE "^[[:space:]]*goals[[:space:]]*=" "$CODEX_CONFIG"; then
  if grep -qE "^[[:space:]]*\[features\]" "$CODEX_CONFIG"; then
    echo "bootstrap: warn — existing [features] block in $CODEX_CONFIG; add goals = true manually" >&2
  else
    printf "\n[features]\ngoals = true\n" >> "$CODEX_CONFIG"
  fi
fi
chmod 0644 "$CODEX_CONFIG"
' || echo "bootstrap: codex config write failed (non-fatal)" >&2

# --- free-tier Codex provider (ENG-4785) ----------------------------------
# Ship an INERT `browser-use-free` provider + profile in ~/.codex/config.toml
# that routes Codex through the cloud control-plane proxy to DeepSeek V4 on
# OpenRouter. It is deliberately NOT the default model_provider — `codex exec`
# reads the default and ignores --profile, so making it default here would
# silently route own-sub users through us. The cloud flips it on per-box by
# writing a top-level `profile = "browser-use-free"` line (codex_use_free WS
# command) only when the user picks the "no sub" option in setup.
#
# base_url points at the control plane (NOT openrouter.ai) so the CP holds the
# OpenRouter key server-side; the box authenticates with its box token, which
# the `bu-cp-token` helper (below) hands to codex via auth.command. Idempotent:
# skipped if the provider block is already present.
#
# base_url = BUX_CP_CODEX_URL: the PrivateLink interface-endpoint DNS in front
# of the CP Codex proxy. The CP is internal-only — it is NOT reachable at the
# public BUX_CLOUD_URL host (that routes to the public API backend, which has
# no /api/codex route). Only the box VPC can reach the proxy, over PrivateLink.
# So we use the endpoint DNS the provisioner wrote into /etc/bux/env, NOT a
# derivation of BUX_CLOUD_URL. Parse only that one key (don't source the file —
# sourcing as root would execute a tampered line).
BUX_CP_CODEX_URL=''
if [ -f /etc/bux/env ]; then
  BUX_CP_CODEX_URL="$(grep -E '^BUX_CP_CODEX_URL=' /etc/bux/env | tail -n1 | cut -d= -f2- | tr -d '"'\''' || true)"
fi
# Empty (not configured for this env) -> skip writing the free-Codex provider.
if [ -z "$BUX_CP_CODEX_URL" ]; then
  echo "bootstrap: BUX_CP_CODEX_URL not set; skipping free-tier Codex provider" >&2
else
# Normalize the scheme: Codex needs an absolute https:// base_url. The value is
# meant to be a full URL, but tolerate a bare host (or ws/wss left over from a
# misconfigured SSM value) by coercing to https:// rather than emitting an
# invalid scheme-less base_url that silently breaks routing.
case "$BUX_CP_CODEX_URL" in
  https://*) ;;
  http://*)  ;;
  wss://*)   BUX_CP_CODEX_URL="https://${BUX_CP_CODEX_URL#wss://}" ;;
  ws://*)    BUX_CP_CODEX_URL="http://${BUX_CP_CODEX_URL#ws://}" ;;
  *)         BUX_CP_CODEX_URL="https://${BUX_CP_CODEX_URL}" ;;
esac
CP_BASE="${BUX_CP_CODEX_URL%/}/api/codex/v1"
# Self-healing rewrite of the free-Codex config (ENG-4785). The old "append
# only if the provider header is absent" guard left boxes stuck: a partial or
# old write (profile table missing, or stale wire_api = "chat") was never
# repaired, surfacing in Codex as "profile browser-use-free not found" or a
# wire_api load error. We instead strip every existing browser-use-free table
# (provider, its .auth sub-table, and the profile) and re-append fresh ones on
# every bootstrap, so an outdated box self-heals on the next /update.
#
# Done in Python (run as the bux user) rather than awk-inside-bash-c, which is
# a nested-quote minefield. CP_BASE is passed via env so it isn't interpolated
# into the script text.
sudo -u bux -H BUX_CP_BASE="$CP_BASE" python3 - <<'PYEOF' || \
  echo "bootstrap: codex free-tier provider write failed (non-fatal)" >&2
import os
from pathlib import Path

cfg = Path.home() / ".codex" / "config.toml"
cfg.parent.mkdir(parents=True, exist_ok=True)
existing = cfg.read_text(encoding="utf-8") if cfg.exists() else ""

# Drop existing browser-use-free tables: from each matching [header] line until
# the next [header] or EOF. Non-matching tables (e.g. [features]) and top-level
# keys (incl. the `profile` selector codex_use_free sets) are preserved.
strip_headers = (
    "[model_providers.browser-use-free]",
    "[model_providers.browser-use-free.auth]",
    "[profiles.browser-use-free]",
)
out, skip = [], False
for line in existing.splitlines():
    s = line.strip()
    if s.startswith("["):
        skip = s in strip_headers
    if not skip:
        out.append(line)

base = os.environ["BUX_CP_BASE"]
block = f'''
[model_providers.browser-use-free]
name = "Browser Use free (DeepSeek V4)"
base_url = "{base}"
# Codex removed wire_api = "chat" (Feb 2026); "responses" is the only supported
# value. Codex calls {{base_url}}/responses, which the CP proxy forwards to
# OpenRouter's drop-in Responses API. See ENG-4785.
wire_api = "responses"
stream_idle_timeout_ms = 300000

[model_providers.browser-use-free.auth]
command = "/usr/local/bin/bu-cp-token"
args = []
timeout_ms = 5000
refresh_interval_ms = 300000

[profiles.browser-use-free]
model_provider = "browser-use-free"
model = "deepseek/deepseek-v4-flash"
model_reasoning_effort = "none"
'''

body = "\n".join(out).rstrip("\n")
cfg.write_text((body + "\n" if body else "") + block, encoding="utf-8")
cfg.chmod(0o644)
PYEOF
fi

# bu-cp-token: hands Codex the box token as a bearer for the control-plane
# proxy. Codex's auth.command runs this and uses stdout as the token. Reading
# from /etc/bux/env at call time means token rotation is picked up without
# rewriting config.toml.
cat > /usr/local/bin/bu-cp-token <<'TOKENEOF'
#!/usr/bin/env bash
# Print the box token for Codex's control-plane provider auth (ENG-4785).
# Parse only BUX_BOX_TOKEN out of /etc/bux/env — don't source it, so a
# tampered env file can't execute arbitrary shell when codex invokes us.
set -euo pipefail
token=''
if [ -f /etc/bux/env ]; then
  token="$(grep -E '^BUX_BOX_TOKEN=' /etc/bux/env | tail -n1 | cut -d= -f2- | tr -d '"'\''' || true)"
fi
printf '%s' "$token"
TOKENEOF
chmod 0755 /usr/local/bin/bu-cp-token

# --- agent shell helpers --------------------------------------------------
# install.sh creates these symlinks on first boot, but new helpers added to
# agent/ after a box has already been provisioned never get linked into
# /usr/local/bin without a re-bootstrap. Re-assert here on every update so
# the symlinks track agent/ as new helpers ship. Idempotent (ln -sfn).
ln -sfn "$REPO_DIR/agent/tg-send"        /usr/local/bin/tg-send
ln -sfn "$REPO_DIR/agent/tg-buttons"     /usr/local/bin/tg-buttons
ln -sfn "$REPO_DIR/agent/tg-schedule"    /usr/local/bin/tg-schedule
ln -sfn "$REPO_DIR/agent/tg-schedule-fire" /usr/local/bin/tg-schedule-fire
ln -sfn "$REPO_DIR/agent/new-topic"      /usr/local/bin/new-topic
ln -sfn /usr/local/bin/tg-schedule       /usr/local/bin/schedule
ln -sfn "$REPO_DIR/agent/agency-report"  /usr/local/bin/agency-report
ln -sfn "$REPO_DIR/agent/bux-restart"    /usr/local/bin/bux-restart
ln -sfn "$REPO_DIR/agent/bux-miniapp-tunnel" /usr/local/bin/bux-miniapp-tunnel
# Web-terminal agent launcher: picks codex (free-DeepSeek profile or signed-in)
# vs claude, so ttyd doesn't hardcode claude on a codex-only box (ENG-4785).
ln -sfn "$REPO_DIR/agent/bux-agent-shell" /usr/local/bin/bux-agent-shell

# --- system prompt + CLAUDE.md/AGENTS.md symlinks --------------------------
# The one source of truth is /home/bux/system-prompt.md (copied from the
# repo by install.sh). Claude Code reads ~/CLAUDE.md, Codex reads ~/AGENTS.md
# — both symlink to system-prompt.md so editing one file updates both CLIs.
# Re-assert on every update so boxes provisioned with the older "CLAUDE.md
# as the file" layout self-heal to the symlink layout.
if [ -e "$AGENT_DIR/system-prompt.md" ]; then
  install -o bux -g bux -m 0644 "$AGENT_DIR/system-prompt.md" /home/bux/system-prompt.md
  # If a real CLAUDE.md file exists (pre-rename layout), replace it with the symlink.
  if [ -e /home/bux/CLAUDE.md ] && [ ! -L /home/bux/CLAUDE.md ]; then
    rm -f /home/bux/CLAUDE.md
  fi
  ln -sfn /home/bux/system-prompt.md /home/bux/CLAUDE.md
  ln -sfn /home/bux/system-prompt.md /home/bux/AGENTS.md
  chown -h bux:bux /home/bux/CLAUDE.md /home/bux/AGENTS.md
fi

# --- clean up legacy agency-skill stub -------------------------------------
# Before v2, agency was triggered by phrases ("start agency", "scan everything")
# via a Claude Code skill at ~/.claude/skills/agency/SKILL.md. After v2 agency
# is the default; the skill gate is dead. Remove it so it doesn't keep firing
# on old boxes after a git pull.
rm -rf /home/bux/.claude/skills/agency

# Agency DB lives at /var/lib/bux/agency.db (created by agency_db on
# first use). Make sure the directory is writable by `bux` so any
# agency-report invocation can init the schema without sudo.
install -d -o bux -g bux -m 0755 /var/lib/bux
install -d -o bux -g bux -m 0755 /var/lib/bux/miniapp-tunnel
for agency_db_file in /var/lib/bux/agency.db*; do
  [ -e "$agency_db_file" ] || continue
  chown bux:bux "$agency_db_file"
done

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
    echo "bootstrap: registered cloud Composio MCP server (claude)"
  fi
fi

# Same composio MCP for codex. Codex CLI ≥ 0.30 supports HTTP-transport MCP
# servers natively (no mcp-remote bridge needed). `codex mcp add` writes to
# ~/.codex/config.toml. We use --bearer-token-env-var so the token isn't
# baked into the config file — codex reads BUX_BOX_TOKEN at MCP-connect time.
# telegram_bot.py:_build_env forwards BUX_BOX_TOKEN to the codex subprocess.
if [ -z "${BUX_BOX_TOKEN:-}" ]; then
  echo "bootstrap: BUX_BOX_TOKEN not set; skipping codex Composio MCP registration" >&2
elif ! sudo -iu bux command -v codex >/dev/null 2>&1; then
  echo "bootstrap: codex CLI not on PATH; skipping codex Composio MCP registration" >&2
else
  # `sudo -iu bux` (login shell) so per-user PATH from ~/.profile picks up
  # ~/.npm-global/bin where codex actually lives — `-u bux -H` skips that.
  sudo -iu bux codex mcp remove composio >/dev/null 2>&1 || true
  # HTTP-transport MCP servers in codex use `--url <URL>`, not `-- <args>`.
  # The `--` form is for stdio commands. Keep stderr unredirected so any
  # real failure surfaces in install.log instead of getting swallowed.
  if ! ( set +x; sudo -iu bux codex mcp add composio \
        --bearer-token-env-var BUX_BOX_TOKEN \
        --url https://api.browser-use.com/cloud/composio/mcp >/dev/null ); then
    echo "bootstrap: WARN failed to register cloud Composio MCP server for codex" >&2
  fi
  if ! sudo -iu bux codex mcp list 2>/dev/null | grep -q '^composio'; then
    echo "bootstrap: WARN codex composio MCP registration didn't take" >&2
  else
    echo "bootstrap: registered cloud Composio MCP server (codex)"
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
        // bux-browser-keeper / bux-ttyd / bux-miniapp / tunnel: same self-update path.
        if (unit == "bux-tg.service" ||
            unit == "box-agent.service" ||
            unit == "bux-browser-keeper.service" ||
            unit == "bux-miniapp.service" ||
            unit == "bux-miniapp-tunnel.service" ||
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
for unit in box-agent.service bux-ttyd.service bux-browser-keeper.service bux-tg.service bux-miniapp.service bux-miniapp-tunnel.service; do
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
Before=box-agent.service bux-tg.service bux-browser-keeper.service bux-ttyd.service bux-miniapp.service bux-miniapp-tunnel.service

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

# bux-tg is the main UX, enabled-but-conditional on /etc/bux/tg.env.
systemctl enable bux-tg.service

# bux-miniapp + tunnel stay warm so /agency opens immediately after reboot.
# The Mini App API still validates Telegram initData against this box's bot
# token and owner before returning private cards.
systemctl enable bux-miniapp.service
systemctl enable bux-miniapp-tunnel.service

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
if systemctl is-active --quiet bux-miniapp.service; then
  systemctl restart bux-miniapp.service
fi
if systemctl is-active --quiet bux-miniapp-tunnel.service; then
  systemctl restart bux-miniapp-tunnel.service
elif [ -f /etc/bux/tg.env ]; then
  systemctl start bux-miniapp-tunnel.service 2>/dev/null || true
fi
if systemctl is-active --quiet bux-browser-keeper.service; then
  systemctl restart bux-browser-keeper.service
fi
if systemctl is-active --quiet bux-ttyd.service; then
  systemctl restart bux-ttyd.service
fi

echo "bootstrap: done"
