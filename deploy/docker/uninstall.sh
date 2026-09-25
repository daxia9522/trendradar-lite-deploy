#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
source "$SCRIPT_DIR/common.sh"
MODE=stop
case ${1:-} in
  "") ;;
  --purge-data) MODE=purge-data ;;
  --purge-all) MODE=purge-all ;;
  *) fail "Usage: $0 [--purge-data|--purge-all]" ;;
esac
[[ $# -le 1 ]] || fail "Usage: $0 [--purge-data|--purge-all]"
cd "$APP_DIR"
check_docker
if [[ $MODE == stop ]]; then
  docker compose --profile setup down --remove-orphans
  echo "Docker services stopped. Data volume, runtime/env, private backups, .env, image, launcher and clone were preserved."
  exit 0
fi

# Destructive behavior is confined to the explicitly requested purge modes.
# Runtime and backup directories cannot be symlinks into unrelated host paths.
for path in "$APP_DIR/runtime" "$APP_DIR/.env.backups"; do
  [[ ! -L $path ]] || fail "Refusing to purge a symlinked configuration directory."
done
docker compose --profile setup down --volumes --remove-orphans
rm -f -- "$APP_DIR/.env"
rm -rf -- "$APP_DIR/runtime" "$APP_DIR/.env.backups"
remove_launcher
echo "Docker data volume, runtime configuration, legacy .env, private backups and owned launcher removed."
if [[ $MODE == purge-all ]]; then
  docker image rm ghcr.io/daxia9522/trendradar-lite-deploy:latest 2>/dev/null || true
  docker image rm trendradar-lite-deploy:local 2>/dev/null || true
  echo "Local TrendRadar images removed when not used by another container."
fi
echo "The Git clone was preserved. Remove $APP_DIR separately if it is no longer needed."
