#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/trendradar-lite
ENV_FILE=$CONFIG_DIR/env
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
PYTHON_BIN=${PYTHON_BIN:-python3}
ENABLE_TIMERS=true
FORCE_CONFIGURE=false
MODE_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --no-enable) ENABLE_TIMERS=false ;;
    --configure) FORCE_CONFIGURE=true ;;
    --web) MODE_ARGS=(--web) ;;
    *) echo "Usage: $0 [--no-enable] [--configure] [--web]" >&2; exit 2 ;;
  esac
done

command -v "$PYTHON_BIN" >/dev/null || { echo "Python 3 is required." >&2; exit 1; }
"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || {
  echo "Python 3.10 or newer is required." >&2; exit 1;
}

# Configuration-only mode must not install dependencies or change timer enablement.
if [[ $FORCE_CONFIGURE == true ]]; then
  exec "$PYTHON_BIN" "$APP_DIR/deploy/configure.py" --output "$ENV_FILE" --unit-dir "$UNIT_DIR" "${MODE_ARGS[@]}"
fi
command -v systemctl >/dev/null || { echo "systemd is required." >&2; exit 1; }
"$PYTHON_BIN" "$APP_DIR/deploy/native_install.py" check-launcher --output "$ENV_FILE" --unit-dir "$UNIT_DIR"

NEW_INSTALL=false
if [[ ! -e $UNIT_DIR/trendradar-lite.timer && ! -e $UNIT_DIR/trendradar-weekly.timer ]]; then
  NEW_INSTALL=true
fi
if [[ ! -f $ENV_FILE ]]; then
  # No template, directory, launcher, unit or venv is written until confirmed save.
  "$PYTHON_BIN" "$APP_DIR/deploy/configure.py" --output "$ENV_FILE" --unit-dir "$UNIT_DIR" --install "${MODE_ARGS[@]}"
fi

mkdir -p "$APP_DIR/output"
if [[ ! -x $APP_DIR/.venv/bin/python ]]; then
  if ! "$PYTHON_BIN" -m venv "$APP_DIR/.venv"; then
    echo "Failed to create a virtual environment. Install python3-venv first." >&2
    exit 1
  fi
fi
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

"$PYTHON_BIN" "$APP_DIR/deploy/native_install.py" install-units --output "$ENV_FILE" --unit-dir "$UNIT_DIR"
"$PYTHON_BIN" "$APP_DIR/deploy/native_install.py" install-launcher --output "$ENV_FILE" --unit-dir "$UNIT_DIR"
if [[ $NEW_INSTALL == true && $ENABLE_TIMERS == true ]]; then
  # Only a first installation explicitly enables timers. Maintenance preserves state.
  systemctl --user enable --now trendradar-lite.timer trendradar-weekly.timer
fi

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "Warning: $HOME/.local/bin is not on PATH. Shell startup files were not modified."
     echo "Use $HOME/.local/bin/trendradar, or add that directory to PATH yourself." ;;
esac

echo "Native Linux installation completed. No news, mail or AI test was run."
echo "Environment: $ENV_FILE"
echo "Reconfigure: $HOME/.local/bin/trendradar"
echo "Status (explicit local diagnostics): $APP_DIR/deploy/linux/status.sh"
