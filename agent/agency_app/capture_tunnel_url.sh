#!/usr/bin/env bash
# Scrape the latest https://*.trycloudflare.com URL out of the tunnel log
# and write it into /etc/bux/env as BUX_AGENCY_APP_URL.
#
# cloudflared's quick tunnels don't expose the URL via a stable API — it
# lands as a banner line on stderr at boot:
#   |  https://abc-def-ghi.trycloudflare.com                                |
# We poll up to ~60s waiting for it to appear, then write the env file
# under root + chgrp bux 640. /etc/bux is the bux group's writable cache.
set -euo pipefail

LOG=/var/lib/bux/agency-tunnel/log
ENV_FILE=/etc/bux/env

for _ in $(seq 1 60); do
  url=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -n1 || true)
  if [ -n "$url" ]; then
    if grep -q '^BUX_AGENCY_APP_URL=' "$ENV_FILE" 2>/dev/null; then
      sed -i "s|^BUX_AGENCY_APP_URL=.*|BUX_AGENCY_APP_URL=$url|" "$ENV_FILE"
    else
      printf 'BUX_AGENCY_APP_URL=%s\n' "$url" >> "$ENV_FILE"
    fi
    chmod 640 "$ENV_FILE"
    chown root:bux "$ENV_FILE"
    echo "agency-tunnel-url: captured $url"
    exit 0
  fi
  sleep 1
done

echo "agency-tunnel-url: timed out waiting for trycloudflare URL" >&2
exit 1
