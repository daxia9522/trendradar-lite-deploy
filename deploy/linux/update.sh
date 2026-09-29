#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

if [[ ! -d $APP_DIR/.git ]]; then
  echo "Update requires a Git clone: $APP_DIR" >&2
  exit 1
fi
if ! tracked_status=$(git -C "$APP_DIR" status --short --untracked-files=no); then
  echo "Cannot inspect tracked files; update aborted." >&2
  exit 1
fi
if [[ -n $tracked_status ]]; then
  echo "Tracked files contain local changes; update aborted." >&2
  exit 1
fi

# Separate fetch and merge so pull.rebase/pull.ff never change update semantics.
# Resolve the configured upstream, rather than assuming origin/main. A missing
# upstream, failed fetch, or divergent history exits before reinstalling.
git -C "$APP_DIR" rev-parse --verify '@{upstream}' >/dev/null
git -C "$APP_DIR" fetch --no-recurse-submodules
git -C "$APP_DIR" merge --ff-only --no-autostash '@{upstream}'
"$APP_DIR/deploy/linux/install.sh" --no-enable
echo "Update completed; existing timer enablement was preserved."
