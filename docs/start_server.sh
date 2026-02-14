#!/usr/bin/env bash
set -euo pipefail

PORT=18791
LOG_FILE="/tmp/dashboard_server.log"
UPDATER_LOG="/tmp/dashboard_updater.log"
DASHBOARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATER_SCRIPT="${DASHBOARD_DIR}/realtime_updater.py"

is_listening() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn "sport = :${PORT}" 2>/dev/null | grep -q "LISTEN"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1
  else
    # Fallback: best-effort via /proc
    grep -qi ":$(printf '%04X' "${PORT}")" /proc/net/tcp 2>/dev/null
  fi
}

is_updater_running() {
  pgrep -f "python3 .*realtime_updater\.py" >/dev/null 2>&1
}

cd "${DASHBOARD_DIR}"

# Start realtime updater (background) if needed
if [[ -f "${UPDATER_SCRIPT}" ]]; then
  if ! is_updater_running; then
    nohup python3 "${UPDATER_SCRIPT}" >>"${UPDATER_LOG}" 2>&1 &
  fi
fi

# Start http server in background if needed
if ! is_listening; then
  nohup python3 -m http.server "${PORT}" --bind 0.0.0.0 >>"${LOG_FILE}" 2>&1 &

  # Give it a moment to bind
  sleep 0.2

  if ! is_listening; then
    echo "Failed to start dashboard server on port ${PORT}. Check ${LOG_FILE}" >&2
    exit 1
  fi
fi
