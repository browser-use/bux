#!/usr/bin/env bash
# bux install — set up Claude Code + Browser Use Cloud browser + optional
# Telegram bot on a fresh Ubuntu / Debian box.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/browser-use/bux/main/install.sh \
#     | sudo BROWSER_USE_API_KEY=bu_xxx bash
#
# Or clone + run locally:
#   git clone https://github.com/browser-use/bux && cd bux && sudo ./install.sh
#
# BUX_REF (default: main) controls which ref the curl-pipe installer pulls
# from. Set it to a commit sha if you want to pin:
#   curl … | sudo BUX_REF=<sha> BROWSER_USE_API_KEY=bu_xxx bash
#
# Re-running the script is idempotent. It will reuse existing tokens and
# configuration; delete /etc/bux/ to start clean.
set -euo pipefail

BUX_REF="${BUX_REF:-main}"

# --- pretty output ---------------------------------------------------------
c_bold=$'\033[1m'; c_dim=$'\033[2m'; c_green=$'\033[32m'; c_red=$'\033[31m'; c_reset=$'\033[0m'
say()  { printf '%s➜%s %s\n' "$c_bold" "$c_reset" "$*"; }
ok()   { printf '%s✓%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '%s!%s %s\n' "$c_red" "$c_reset" "$*" >&2; }
die()  { warn "$*"; exit 1; }

[ "$EUID" -eq 0 ] || die 'must run as root (use sudo)'
[ -f /etc/debian_version ] || die 'only debian/ubuntu is supported'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# If the script was piped through curl, BASH_SOURCE[0] is /dev/stdin — in that
# case we fetch the rest of the repo from github at $BUX_REF. GitHub's
# archive/<ref>.tar.gz endpoint accepts branches, tags, and commit SHAs.
if [ "$REPO_DIR" = '/dev' ] || [ ! -f "$REPO_DIR/agent/browser_keeper.py" ]; then
	say "fetching bux@${BUX_REF} from github"
	tmpdir="$(mktemp -d)"
	curl -fsSL "https://github.com/browser-use/bux/archive/${BUX_REF}.tar.gz" \
		| tar -xz -C "$tmpdir" --strip-components=1 \
		|| die "failed to download bux@${BUX_REF}"
	REPO_DIR="$tmpdir"
fi

# --- collect config --------------------------------------------------------
BROWSER_USE_API_KEY="${BROWSER_USE_API_KEY:-}"
BUX_PROFILE_ID="${BUX_PROFILE_ID:-}"

# If /etc/bux/env already exists (rerun), seed missing values from it so the
# script is truly idempotent without making the user re-type secrets.
if [ -z "$BROWSER_USE_API_KEY" ] && [ -r /etc/bux/env ]; then
	# shellcheck disable=SC1091
	BROWSER_USE_API_KEY="$(. /etc/bux/env && printf %s "${BROWSER_USE_API_KEY:-}")"
	# shellcheck disable=SC1091
	BUX_PROFILE_ID="${BUX_PROFILE_ID:-$(. /etc/bux/env && printf %s "${BUX_PROFILE_ID:-}")}"
fi

if [ -z "$BROWSER_USE_API_KEY" ] && [ -t 0 ]; then
	printf '%sBROWSER_USE_API_KEY%s (get one at https://cloud.browser-use.com/new-api-key): ' "$c_bold" "$c_reset"
	read -r BROWSER_USE_API_KEY
fi
[ -n "$BROWSER_USE_API_KEY" ] || die 'BROWSER_USE_API_KEY is required (export it or pass via env)'
TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"

# --- base packages ---------------------------------------------------------
say 'installing system packages'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
	curl git build-essential python3 python3-pip python3-venv \
	unzip ca-certificates jq gnupg \
	ripgrep fd-find python3-dev make gcc g++ pkg-config libssl-dev zlib1g-dev \
	htop tmux vim less wget zip tree \
	at

# Enable atd so `at now + 5min` actually runs queued jobs.
systemctl enable --now atd.service 2>/dev/null || true

# Allow the bux user to use `at` (Ubuntu's default at.deny excludes
# regular users; at.allow is presence-implies-deny-for-everyone-else).
echo bux > /etc/at.allow
chmod 644 /etc/at.allow

arch="$(uname -m)"

# --- gh (GitHub CLI) -------------------------------------------------------
if ! command -v gh >/dev/null 2>&1; then
	say 'installing GitHub CLI'
	install -d -m 0755 /etc/apt/keyrings
	rm -f /etc/apt/keyrings/githubcli-archive-keyring.gpg
	curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
		-o /etc/apt/keyrings/githubcli-archive-keyring.gpg
	chmod 644 /etc/apt/keyrings/githubcli-archive-keyring.gpg
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
		> /etc/apt/sources.list.d/github-cli.list
	apt-get update -qq
	apt-get install -y -qq gh
fi

# --- uv (fast Python package manager) -------------------------------------
# Pinned + SHA-verified release tarball. Same threat model as ttyd / nodejs:
# never `curl … | sh` as root, since one compromised redirect on astral.sh
# would execute arbitrary code on every install.
UV_VERSION='0.11.7'
case "$arch" in
	x86_64)
		uv_arch='x86_64-unknown-linux-gnu'
		UV_SHA256='6681d691eb7f9c00ac6a3af54252f7ab29ae72f0c8f95bdc7f9d1401c23ea868'
		;;
	aarch64|arm64)
		uv_arch='aarch64-unknown-linux-gnu'
		UV_SHA256='f2ee1cde9aabb4c6e43bd3f341dadaf42189a54e001e521346dc31547310e284'
		;;
	*) die "unsupported arch for uv: $arch" ;;
esac
if ! command -v uv >/dev/null 2>&1 || [ "$(uv --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]; then
	say 'installing uv'
	tmp_uv="$(mktemp -d)"
	curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${uv_arch}.tar.gz" \
		-o "$tmp_uv/uv.tgz"
	got_sha=$(sha256sum "$tmp_uv/uv.tgz" | awk '{print $1}')
	if [ "$got_sha" != "$UV_SHA256" ]; then
		rm -rf "$tmp_uv"
		die "uv SHA mismatch: got $got_sha"
	fi
	tar -xzf "$tmp_uv/uv.tgz" -C "$tmp_uv"
	install -m 0755 "$tmp_uv/uv-${uv_arch}/uv"  /usr/local/bin/uv
	install -m 0755 "$tmp_uv/uv-${uv_arch}/uvx" /usr/local/bin/uvx
	rm -rf "$tmp_uv"
fi

# --- Node.js 24 LTS via NodeSource (GPG-pinned) ----------------------------
if ! node --version 2>/dev/null | grep -q '^v24'; then
	say 'installing Node.js 24 LTS'
	NODESOURCE_KEY_FPR='6F71F525282841EEDAF851B42F59B5F99B1BE0B4'
	install -d -m 0755 /etc/apt/keyrings
	rm -f /etc/apt/keyrings/nodesource.gpg
	curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
		| gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
	got_fpr=$(gpg --no-default-keyring --keyring /etc/apt/keyrings/nodesource.gpg \
		--list-keys --with-colons | awk -F: '/^fpr:/ {print $10; exit}')
	[ "$got_fpr" = "$NODESOURCE_KEY_FPR" ] || die "NodeSource GPG mismatch: $got_fpr"
	echo 'deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main' \
		> /etc/apt/sources.list.d/nodesource.list
	apt-get update -qq
	apt-get install -y -qq nodejs
fi

# --- Claude Code -----------------------------------------------------------
if ! command -v claude >/dev/null 2>&1; then
	say 'installing Claude Code'
	npm install -g @anthropic-ai/claude-code
fi

# --- bux user + dirs -------------------------------------------------------
id -u bux >/dev/null 2>&1 || useradd -m -s /bin/bash bux
mkdir -p /opt/bux /var/log/bux /etc/bux /home/bux/.claude/skills
chown -R bux:bux /opt/bux /home/bux/.claude /var/log/bux
chown root:bux /etc/bux
chmod 2775 /etc/bux

# NOTE: we deliberately do NOT grant the bux user passwordless sudo, even
# scoped. `sudo apt install <arbitrary deb>` runs maintainer scripts as root
# and is therefore root-equivalent — same for dpkg, npm, pipx, snap. Anything
# we'd whitelist here breaks the boundary that keeps the TG bot's bot-token
# (root-owned at /etc/bux/tg.env) safe from a compromised bux user.
#
# Users get tools two ways: (1) the apt packages baked in here, and
# (2) per-user installs into $HOME via uv / pipx / npm-prefix / pyenv. If
# something's missing, add it to the apt list above and re-run the installer.
rm -f /etc/sudoers.d/bux-dev  # in case an earlier install left one

# --- SSH access for the bux user -------------------------------------------
# We pre-create ~/.ssh and lock sshd to pubkey-only. Users who want ssh paste
# their pubkey into ~/.ssh/authorized_keys themselves (via the web terminal,
# `sudo -iu bux`, or whatever). No keys are seeded here, so opening port 22
# at the cloud/firewall level + sshd does NOT mean anyone can log in until
# the user adds their own key.
# Reject symlinks before touching ssh paths. /home/bux is bux-owned, so on
# a rerun the bux user could symlink ~/.ssh → /etc or ~/.ssh/authorized_keys
# → /etc/shadow and our root chown/chmod would follow the link. -L matches
# even dangling links; we don't allow symlinks here at all.
for p in /home/bux/.ssh /home/bux/.ssh/authorized_keys; do
	if [ -L "$p" ]; then
		die "refusing to operate on symlinked $p"
	fi
done

install -d -o bux -g bux -m 0700 /home/bux/.ssh
# Don't clobber an existing authorized_keys, but always re-assert ownership
# + mode — sshd silently ignores pubkeys when authorized_keys has wrong
# perms, and a previous install or manual edit may have left it 0644.
# -f means regular file; refuse to operate on dirs/sockets/FIFOs (the -L
# check above already handled symlinks).
if [ -e /home/bux/.ssh/authorized_keys ] && [ ! -f /home/bux/.ssh/authorized_keys ]; then
	die '/home/bux/.ssh/authorized_keys exists but is not a regular file'
fi
if [ ! -f /home/bux/.ssh/authorized_keys ]; then
	install -o bux -g bux -m 0600 /dev/null /home/bux/.ssh/authorized_keys
fi
# -h on chown for symlink TOCTOU defense in depth. chmod has no -h variant
# but the -L test above already rejected symlinks.
chown -h bux:bux /home/bux/.ssh/authorized_keys
chmod 0600 /home/bux/.ssh/authorized_keys

cat > /etc/ssh/sshd_config.d/00-bux.conf <<'SSHD'
# bux: pubkey only, no passwords, no root login.
PasswordAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
KbdInteractiveAuthentication no
SSHD
chmod 644 /etc/ssh/sshd_config.d/00-bux.conf

# Pick whichever unit name the distro ships (Ubuntu = ssh.service, RHEL-likes
# = sshd.service) and fail clearly if neither exists. Don't swallow errors —
# a botched ssh enable/reload should surface, not be hidden.
ssh_unit=''
for u in ssh.service sshd.service; do
	if systemctl list-unit-files "$u" --no-legend --no-pager 2>/dev/null | grep -q .; then
		ssh_unit="$u"
		break
	fi
done
[ -n "$ssh_unit" ] || die 'no ssh unit found (ssh.service / sshd.service)'
systemctl enable "$ssh_unit"
# `reload` requires the unit to be running already. Fall back to `restart`
# if reload fails (e.g. fresh box where sshd is enabled but not yet started).
systemctl reload "$ssh_unit" || systemctl restart "$ssh_unit"

# --- Python venv for the agent ---------------------------------------------
if [ ! -d /opt/bux/venv ]; then
	sudo -u bux python3 -m venv /opt/bux/venv
fi
sudo -u bux /opt/bux/venv/bin/pip install --quiet --upgrade pip
sudo -u bux /opt/bux/venv/bin/pip install --quiet websockets httpx

# --- browser-harness-js skill ---------------------------------------------
if [ ! -d /home/bux/.claude/skills/cdp ]; then
	say 'installing browser-harness-js skill'
	sudo -u bux git clone --depth=1 \
		https://github.com/browser-use/browser-harness-js \
		/home/bux/.claude/skills/cdp
fi
if [ -f /home/bux/.claude/skills/cdp/sdk/browser-harness-js ]; then
	ln -sf /home/bux/.claude/skills/cdp/sdk/browser-harness-js /usr/local/bin/browser-harness-js
	chmod +x /home/bux/.claude/skills/cdp/sdk/browser-harness-js
fi

# --- ttyd (web terminal, localhost only) -----------------------------------
# Per-arch SHA256 from https://github.com/tsl0922/ttyd/releases/tag/1.7.7.
# Never use `latest` for binaries you exec as root.
TTYD_VERSION='1.7.7'
case "$arch" in
	x86_64)
		ttyd_arch=x86_64
		TTYD_SHA256='8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55'
		;;
	aarch64|arm64)
		ttyd_arch=aarch64
		TTYD_SHA256='b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165'
		;;
	*) die "unsupported arch: $arch" ;;
esac

# Skip reinstall if the binary is already the expected build. Checking the
# SHA directly is more reliable than parsing `ttyd --version` (output format
# has shifted across releases) and keeps reruns cheap.
installed_sha=''
if [ -f /usr/local/bin/ttyd ]; then
	installed_sha=$(sha256sum /usr/local/bin/ttyd | awk '{print $1}')
fi
if [ "$installed_sha" != "$TTYD_SHA256" ]; then
	say 'installing ttyd'
	# Download to a tempfile and mv into place. Writing directly to
	# /usr/local/bin/ttyd fails on rerun because bux-ttyd.service has the
	# current binary open — curl -o truncates, OS refuses for a running ELF.
	tmp_ttyd="$(mktemp)"
	curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${ttyd_arch}" \
		-o "$tmp_ttyd"
	got_sha=$(sha256sum "$tmp_ttyd" | awk '{print $1}')
	if [ "$got_sha" != "$TTYD_SHA256" ]; then
		rm -f "$tmp_ttyd"
		die "ttyd SHA mismatch: got $got_sha"
	fi
	chmod +x "$tmp_ttyd"
	# `mv` over a running binary is safe (unlinks the old inode, creates new).
	mv "$tmp_ttyd" /usr/local/bin/ttyd
	systemctl restart bux-ttyd.service 2>/dev/null || true
fi

# --- drop agent files ------------------------------------------------------
say 'installing bux agent files'
install -o bux -g bux -m 0644 "$REPO_DIR/agent/browser_keeper.py" /opt/bux/browser_keeper.py
install -o bux -g bux -m 0644 "$REPO_DIR/agent/telegram_bot.py"   /opt/bux/telegram_bot.py
install -o bux -g bux -m 0644 "$REPO_DIR/agent/CLAUDE.md"         /home/bux/CLAUDE.md

# --- tg-send: shell helper to push a message to the bound TG chat ---------
# Used by `at` / cron jobs (and claude from a shell) so scheduled work can
# notify the user without going through the bot's poll loop. The bot token
# lives at /etc/bux/tg.env (mode 640 root:bux — the bux user can read it,
# the helper runs as bux, no setuid magic needed).
cat > /usr/local/bin/tg-send <<'TGSEND'
#!/usr/bin/env bash
# tg-send "your message here"
# Posts a Telegram message to the bound chat. Used by scheduled jobs:
#   echo 'tg-send "reminder"' | at now + 5min
set -euo pipefail
[ "$#" -ge 1 ] || { echo "usage: tg-send <message>" >&2; exit 2; }
text="$*"
env_file=/etc/bux/tg.env
allow_file=/etc/bux/tg-allowed.txt
[ -r "$env_file" ] || { echo "tg-send: cannot read $env_file" >&2; exit 1; }
[ -r "$allow_file" ] || { echo "tg-send: no bound chat (run /start in TG first)" >&2; exit 1; }
# shellcheck disable=SC1090
. "$env_file"
[ -n "${TG_BOT_TOKEN:-}" ] || { echo "tg-send: TG_BOT_TOKEN missing" >&2; exit 1; }
chat_id=$(awk 'NF{print; exit}' "$allow_file")
[ -n "$chat_id" ] || { echo "tg-send: empty $allow_file" >&2; exit 1; }
curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
  --max-time 15 \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg c "$chat_id" --arg t "$text" '{chat_id: ($c|tonumber), text: $t}')" \
  > /dev/null
TGSEND
chmod 755 /usr/local/bin/tg-send

# --- pre-seed ~/.claude.json so first `claude` run skips dialogs -----------
if [ ! -f /home/bux/.claude.json ]; then
	sudo -u bux -H bash -c 'cat > /home/bux/.claude.json' <<'JSON'
{
  "hasCompletedOnboarding": true,
  "theme": "dark",
  "hasSeenTasksHint": true,
  "bypassPermissionsModeAccepted": true,
  "projects": {
    "/home/bux": {
      "hasTrustDialogAccepted": true,
      "hasCompletedProjectOnboarding": true,
      "projectOnboardingSeenCount": 1,
      "allowedTools": [],
      "mcpContextUris": [],
      "mcpServers": {},
      "enabledMcpjsonServers": [],
      "disabledMcpjsonServers": []
    }
  }
}
JSON
	chmod 600 /home/bux/.claude.json
	chown bux:bux /home/bux/.claude.json
fi

# --- /etc/bux/env (shared by systemd services) -----------------------------
if [ ! -f /etc/bux/env ]; then
	cat > /etc/bux/env <<EOF
BROWSER_USE_API_KEY=$BROWSER_USE_API_KEY
BUX_PROFILE_ID=$BUX_PROFILE_ID
EOF
	chmod 640 /etc/bux/env
	chown root:bux /etc/bux/env
else
	say 'keeping existing /etc/bux/env (delete it to regenerate)'
fi

# --- guard against symlinked dotfiles before we chown / append -------------
# /home/bux is bux-owned. Without this, a malicious bux user could symlink
# ~/.bashrc or ~/.profile to a root-owned path and our cat>>/chown would
# follow the link. -L matches dangling symlinks too.
for p in /home/bux/.bashrc /home/bux/.profile; do
	if [ -L "$p" ]; then
		die "refusing to operate on symlinked $p"
	fi
	if [ -e "$p" ] && [ ! -f "$p" ]; then
		die "refusing to operate on non-regular-file $p"
	fi
done

# --- auto-source browser env in bux's shell --------------------------------
if ! grep -q 'browser.env' /home/bux/.bashrc 2>/dev/null; then
	cat >> /home/bux/.bashrc <<'BASHRC'

# Auto-source Browser Use env written by the browser-keeper.
[ -f "$HOME/.claude/browser.env" ] && . "$HOME/.claude/browser.env" 2>/dev/null || true
BASHRC
	chown bux:bux /home/bux/.bashrc
fi

# --- per-user PATH so bux can upgrade their own tools without root ---------
# Goes in .profile (login shells / ssh) since .bashrc bails for
# non-interactive shells. .npm-global/bin shadows /usr/bin/<pkg>,
# .local/bin covers uv/pipx.
if ! grep -q 'npm-global' /home/bux/.profile 2>/dev/null; then
	cat >> /home/bux/.profile <<'PROFILE'

# Per-user installs shadow system ones (gh/uv/etc.) so the bux user can
# upgrade their own tools without root.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
PROFILE
	chown bux:bux /home/bux/.profile
fi

# Pre-set npm global prefix so `npm install -g <pkg>` lands in ~/.npm-global
# without root.
sudo -u bux -H npm config set prefix /home/bux/.npm-global 2>/dev/null || true
install -d -o bux -g bux -m 0755 /home/bux/.npm-global /home/bux/.local /home/bux/.local/bin

# --- login banner: print live browser URL on each ssh login ---------------
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

# --- systemd units ---------------------------------------------------------
cat > /etc/systemd/system/bux-browser-keeper.service <<'UNIT'
[Unit]
Description=bux browser-keeper (long-lived Browser Use Cloud browser)
After=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/bux/env

[Service]
Type=simple
User=bux
Group=bux
EnvironmentFile=/etc/bux/env
WorkingDirectory=/opt/bux
ExecStart=/opt/bux/venv/bin/python /opt/bux/browser_keeper.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/bux/keeper.log
StandardError=append:/var/log/bux/keeper.log

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/bux-ttyd.service <<'UNIT'
[Unit]
Description=bux ttyd web terminal (localhost only)
After=network-online.target

[Service]
Type=simple
User=bux
Group=bux
ExecStart=/usr/local/bin/ttyd -i lo -p 7681 -W /usr/bin/claude
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/bux-tg.service <<'UNIT'
[Unit]
Description=bux Telegram bot
After=network-online.target
ConditionPathExists=/etc/bux/tg.env

[Service]
Type=simple
User=root
Group=root
EnvironmentFile=-/etc/bux/tg.env
WorkingDirectory=/opt/bux
ExecStart=/opt/bux/venv/bin/python /opt/bux/telegram_bot.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/bux/tg.log
StandardError=append:/var/log/bux/tg.log

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable bux-browser-keeper.service bux-ttyd.service >/dev/null

# --- optional: Telegram bot setup -----------------------------------------
if [ -n "$TG_BOT_TOKEN" ]; then
	say 'configuring Telegram bot'
	setup_token="$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
	cat > /etc/bux/tg.env <<EOF
TG_BOT_TOKEN=$TG_BOT_TOKEN
TG_SETUP_TOKEN=$setup_token
EOF
	# 0o640 root:bux so the tg-send helper can read the bot token from
	# `at` jobs running as bux. Worst-case leak: someone with bux access
	# can call sendMessage, but only the bound chat receives it — they
	# can't spam arbitrary users.
	chmod 640 /etc/bux/tg.env
	chown root:bux /etc/bux/tg.env
	systemctl enable bux-tg.service >/dev/null
	systemctl restart bux-tg.service

	# Resolve bot username for the user-facing instructions.
	bot_username=$(curl -fsSL "https://api.telegram.org/bot${TG_BOT_TOKEN}/getMe" | jq -r '.result.username' 2>/dev/null || echo '')
	printf '\n%sTelegram bot is live.%s\n' "$c_bold" "$c_reset"
	if [ -n "$bot_username" ] && [ "$bot_username" != 'null' ]; then
		printf '  Open https://t.me/%s and send any message to bind your account.\n' "$bot_username"
	else
		printf '  Open the bot in Telegram and send any message to bind your account.\n'
	fi
	printf '  (The first message wins — nobody else can bind after that.)\n'
fi

# --- start everything ------------------------------------------------------
systemctl restart bux-browser-keeper.service bux-ttyd.service

# --- final summary ---------------------------------------------------------
echo
ok 'bux is installed.'
echo
printf '%sNext:%s\n' "$c_bold" "$c_reset"
printf '  • Become the %sbux%s user and launch Claude Code:\n' "$c_bold" "$c_reset"
printf '      %ssudo -iu bux%s\n' "$c_dim" "$c_reset"
printf '      %scd ~ && claude%s\n' "$c_dim" "$c_reset"
printf '  • First run: type %s/login%s in Claude Code and complete the OAuth flow.\n' "$c_bold" "$c_reset"
printf '  • The browser is already running — check: %scat /home/bux/.claude/browser.env%s\n' "$c_dim" "$c_reset"
echo
if [ -z "$TG_BOT_TOKEN" ]; then
	printf '  %s(optional)%s Add a Telegram bot: create one via @BotFather, then:\n' "$c_dim" "$c_reset"
	printf '      %sTG_BOT_TOKEN=<token> sudo ./install.sh%s\n' "$c_dim" "$c_reset"
	echo
fi
