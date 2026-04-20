#!/usr/bin/env bash
set -euo pipefail

PANEL_URL="${KILN_PANEL_URL:-http://127.0.0.1:8080/panel}"
KIOSK_PROFILE_DIR="${KILN_KIOSK_PROFILE_DIR:-$HOME/.config/kiln-panel-kiosk}"

find_browser() {
  if command -v chromium-browser >/dev/null 2>&1; then
    command -v chromium-browser
    return
  fi
  if command -v chromium >/dev/null 2>&1; then
    command -v chromium
    return
  fi
  if command -v google-chrome >/dev/null 2>&1; then
    command -v google-chrome
    return
  fi
  return 1
}

BROWSER_BIN="$(find_browser || true)"
if [[ -z "$BROWSER_BIN" ]]; then
  echo "No supported Chromium browser found." >&2
  exit 1
fi

mkdir -p "$KIOSK_PROFILE_DIR"

# Wait briefly for X and the local dashboard to become ready on boot.
for _ in $(seq 1 60); do
  if curl --silent --fail --max-time 2 "$PANEL_URL" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if command -v xset >/dev/null 2>&1; then
  xset s off || true
  xset -dpms || true
  xset s noblank || true
fi

if command -v unclutter >/dev/null 2>&1; then
  pkill -x unclutter >/dev/null 2>&1 || true
  unclutter -idle 0.5 -root >/dev/null 2>&1 &
fi

exec "$BROWSER_BIN" \
  --kiosk \
  --app="$PANEL_URL" \
  --window-position=0,0 \
  --start-fullscreen \
  --no-first-run \
  --no-default-browser-check \
  --disable-restore-session-state \
  --disable-session-crashed-bubble \
  --disable-infobars \
  --disable-features=Translate,MediaRouter,AutofillServerCommunication \
  --check-for-update-interval=31536000 \
  --overscroll-history-navigation=0 \
  --user-data-dir="$KIOSK_PROFILE_DIR"
