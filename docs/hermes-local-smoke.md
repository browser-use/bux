# Hermes Local Smoke Test

This exercises the Bux Hermes integration without touching a production Box,
systemd unit, `/home/bux`, `/etc/bux`, or a real Hermes account.

Run from the Bux repo root:

```bash
tmp=$(mktemp -d)
mkdir -p "$tmp/home/.claude" "$tmp/home/.hermes" "$tmp/bin"

printf 'BU_CDP_WS=local-test-ws\nBU_BROWSER_ID=local-test-browser\nBU_PROFILE_ID=local-test-profile\n' \
  > "$tmp/home/.claude/browser.env"
printf 'existing hermes soul\n' > "$tmp/home/.hermes/SOUL.md"

cat > "$tmp/hermes" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "--version" ]; then echo "fake-hermes 0.1"; exit 0; fi
if [ "${1:-}" = "status" ]; then echo "fake-hermes status ok"; exit 0; fi
if [ "${1:-}" = "--oneshot" ]; then
  shift
  echo "fake-hermes prompt: $*"
  unset BU_BROWSER_ID BU_CDP_WS BU_PROFILE_ID
  browser-harness -c 'ensure_real_tab(); print(page_info()["title"])'
  exit 0
fi
echo "fake-hermes args: $*"
EOF
chmod +x "$tmp/hermes"

BUX_HOME="$tmp/home" BUX_BIN_DIR="$tmp/bin" HERMES_BIN="$tmp/hermes" \
  ./agent/install-hermes

BUX_HOME="$tmp/home" HERMES_BIN="$tmp/hermes" \
  ./agent/bux-hermes --bux-env-check

BUX_HOME="$tmp/home" HERMES_BIN="$tmp/hermes" \
  ./agent/bux-hermes run 'open the browser smoke page'
```

Expected evidence:

- `install-hermes` reports that it linked `bux-hermes` and updated
  `$tmp/home/.hermes/SOUL.md`.
- `$tmp/home/.hermes/SOUL.md` still contains `existing hermes soul` plus the
  Bux managed block.
- `--bux-env-check` prints the temp `BUX_HOME`, `BUX_HERMES_SOUL`,
  `BU_CDP_WS`, `BU_BROWSER_ID`, and `BU_PROFILE_ID`.
- The final wrapper call prints the fake Hermes prompt and a Browser Use page
  title from `browser-harness`.
