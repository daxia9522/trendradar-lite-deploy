#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

if [[ ! -d $APP_DIR/.git ]]; then
  echo "Update requires a Git clone: $APP_DIR" >&2
  exit 1
fi
if [[ -n $(git -C "$APP_DIR" status --short --untracked-files=no) ]]; then
  echo "Tracked files contain local changes; update aborted." >&2
  exit 1
fi

git -C "$APP_DIR" pull --ff-only
"$APP_DIR/deploy/linux/install.sh" --no-enable
echo "Update completed; existing timer enablement was preserved."
