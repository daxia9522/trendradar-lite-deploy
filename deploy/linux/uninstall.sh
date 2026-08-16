#!/usr/bin/env bash
set -euo pipefail

CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
UNIT_DIR=$CONFIG_HOME/systemd/user
PURGE_DATA=false

if [[ ${1:-} == "--purge-data" ]]; then
  PURGE_DATA=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--purge-data]" >&2
  exit 2
fi

systemctl --user disable --now trendradar-lite.timer trendradar-weekly.timer 2>/dev/null || true
rm -f \
  "$UNIT_DIR/trendradar-lite.service" \
  "$UNIT_DIR/trendradar-lite.timer" \
  "$UNIT_DIR/trendradar-weekly.service" \
  "$UNIT_DIR/trendradar-weekly.timer"
systemctl --user daemon-reload

if [[ $PURGE_DATA == true ]]; then
  SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
  rm -rf -- "$APP_DIR/output"
  rm -f -- "$CONFIG_HOME/trendradar-lite/env"
  echo "Runtime data and environment file removed."
else
  echo "Runtime data and environment file were preserved."
fi

echo "systemd user units removed. The Git clone and virtual environment were preserved."
