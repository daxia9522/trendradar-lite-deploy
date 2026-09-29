#!/usr/bin/env bash
set -euo pipefail

CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
UNIT_DIR=$CONFIG_HOME/systemd/user
ENV_FILE=$CONFIG_HOME/trendradar-lite/env
BACKUP_DIR=$CONFIG_HOME/trendradar-lite/.env.backups
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
PURGE_DATA=false

if [[ ${1:-} == "--purge-data" ]]; then
  PURGE_DATA=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--purge-data]" >&2
  exit 2
fi

# Private backups hold replaced secrets. Refuse before changing anything if
# purging them would only drop a symlink and leave its target behind.
if [[ $PURGE_DATA == true && -L $BACKUP_DIR ]]; then
  echo "Refusing to purge a symlinked backup directory." >&2
  exit 2
fi

# Refuse foreign/overridden backup units before touching any existing install.
"$PYTHON_BIN" "$APP_DIR/deploy/native_install.py" remove-backup \
  --output "$ENV_FILE" --unit-dir "$UNIT_DIR"
# Stop/disable failures must leave report units intact. The helper verifies
# timer state and uses the existing safety gate for every explicit reload.
"$PYTHON_BIN" "$APP_DIR/deploy/native_uninstall.py" --unit-dir "$UNIT_DIR"
"$PYTHON_BIN" "$APP_DIR/deploy/native_install.py" remove-launcher \
  --output "$ENV_FILE" --unit-dir "$UNIT_DIR"

if [[ $PURGE_DATA == true ]]; then
  rm -rf -- "$APP_DIR/output"
  rm -f -- "$ENV_FILE"
  rm -rf -- "$BACKUP_DIR"
  echo "Runtime data, environment file and its private backups removed."
else
  echo "Runtime data, environment file and its private backups were preserved."
fi

echo "systemd user units removed. The Git clone and virtual environment were preserved."
