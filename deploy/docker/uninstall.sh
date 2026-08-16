#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
MODE=stop

case ${1:-} in
  "") ;;
  --purge-data) MODE=purge-data ;;
  --purge-all) MODE=purge-all ;;
  *)
    echo "Usage: $0 [--purge-data|--purge-all]" >&2
    exit 2
    ;;
esac

cd "$APP_DIR"

if [[ $MODE == stop ]]; then
  docker compose --profile setup down --remove-orphans
  echo "Docker services stopped. The data volume, private .env, image, and Git clone were preserved."
  exit 0
fi

docker compose --profile setup down --volumes --remove-orphans
rm -f -- "$APP_DIR/.env"
echo "Docker services, data volume, and private .env removed."

if [[ $MODE == purge-all ]]; then
  docker image rm trendradar-lite-deploy:local 2>/dev/null || true
  echo "Local TrendRadar Docker image removed when it was not used by another container."
fi

echo "The Git clone was preserved. Remove $APP_DIR separately if it is no longer needed."
