#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/trendradar-lite
ENV_FILE=$CONFIG_DIR/env

systemctl --user --no-pager status trendradar-lite.timer trendradar-weekly.timer || true
echo
"${PYTHON_BIN:-python3}" "$APP_DIR/deploy/native_install.py" doctor --output "$ENV_FILE"
