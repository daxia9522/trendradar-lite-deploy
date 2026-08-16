#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

systemctl --user --no-pager status trendradar-lite.timer trendradar-weekly.timer || true
echo
(
  cd "$APP_DIR"
  "$APP_DIR/.venv/bin/python" -m trendradar --doctor
)
