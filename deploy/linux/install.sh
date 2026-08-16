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

for arg in "$@"; do
  case "$arg" in
    --no-enable) ENABLE_TIMERS=false ;;
    --configure) FORCE_CONFIGURE=true ;;
    *)
      echo "Usage: $0 [--no-enable] [--configure]" >&2
      exit 2
      ;;
  esac
done

command -v "$PYTHON_BIN" >/dev/null || {
  echo "Python 3 is required." >&2
  exit 1
}
command -v systemctl >/dev/null || {
  echo "systemd is required for the native Linux deployment." >&2
  exit 1
}

"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || {
  echo "Python 3.10 or newer is required." >&2
  exit 1
}

mkdir -p "$CONFIG_DIR" "$UNIT_DIR" "$APP_DIR/output"

NEW_ENV=false
if [[ ! -f $ENV_FILE ]]; then
  install -m 600 "$APP_DIR/.env.example" "$ENV_FILE"
  NEW_ENV=true
  echo "Created $ENV_FILE."
else
  chmod 600 "$ENV_FILE"
  echo "Preserved existing environment file: $ENV_FILE"
fi

if [[ $NEW_ENV == true || $FORCE_CONFIGURE == true ]]; then
  echo "Starting the configuration page. Installation continues after you save it."
  "$PYTHON_BIN" "$APP_DIR/deploy/configure.py" --output "$ENV_FILE"
fi

if [[ ! -x $APP_DIR/.venv/bin/python ]]; then
  if ! "$PYTHON_BIN" -m venv "$APP_DIR/.venv"; then
    rm -rf -- "$APP_DIR/.venv"
    echo "Failed to create a virtual environment." >&2
    echo "Install the Python venv package first (Ubuntu/Debian: apt install python3-venv)." >&2
    exit 1
  fi
fi
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

escape_sed() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

app_escaped=$(escape_sed "$APP_DIR")
env_escaped=$(escape_sed "$ENV_FILE")
python_escaped=$(escape_sed "$APP_DIR/.venv/bin/python")

for name in trendradar-lite trendradar-weekly; do
  sed \
    -e "s|@APP_DIR@|$app_escaped|g" \
    -e "s|@ENV_FILE@|$env_escaped|g" \
    -e "s|@PYTHON@|$python_escaped|g" \
    "$APP_DIR/deploy/systemd/$name.service.in" \
    > "$UNIT_DIR/$name.service"
  chmod 644 "$UNIT_DIR/$name.service"
  install -m 644 "$APP_DIR/deploy/systemd/$name.timer" "$UNIT_DIR/$name.timer"
done

"$PYTHON_BIN" "$APP_DIR/deploy/configure.py" \
  --output "$ENV_FILE" \
  --render-systemd-timer "$UNIT_DIR/trendradar-lite.timer"

systemctl --user daemon-reload
if [[ $ENABLE_TIMERS == true ]]; then
  systemctl --user enable --now trendradar-lite.timer trendradar-weekly.timer
fi

(
  cd "$APP_DIR"
  set -a
  # The setup wizard creates this private shell-compatible environment file.
  source "$ENV_FILE"
  set +a
  "$APP_DIR/.venv/bin/python" -m trendradar --doctor
)

if command -v loginctl >/dev/null; then
  user_name=$(id -un)
  linger=$(loginctl show-user "$user_name" -p Linger --value 2>/dev/null || true)
  if [[ $linger != "yes" ]]; then
    echo "Warning: user lingering is disabled. Timers stop when the user has no session."
    echo "An administrator can enable it with: loginctl enable-linger $user_name"
  fi
fi

echo "Native Linux installation completed."
echo "Environment: $ENV_FILE"
echo "Reconfigure: $APP_DIR/deploy/linux/install.sh --configure"
echo "Status: $APP_DIR/deploy/linux/status.sh"
