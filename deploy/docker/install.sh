#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
FORCE_CONFIGURE=false
START=true

for arg in "$@"; do
  case "$arg" in
    --configure) FORCE_CONFIGURE=true ;;
    --no-start) START=false ;;
    *)
      echo "Usage: $0 [--configure] [--no-start]" >&2
      exit 2
      ;;
  esac
done

command -v docker >/dev/null || {
  echo "Docker is required." >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is required." >&2
  exit 1
}

mkdir -p "$APP_DIR/output"
NEW_ENV=false
if [[ ! -f $APP_DIR/.env ]]; then
  install -m 600 "$APP_DIR/.env.example" "$APP_DIR/.env"
  NEW_ENV=true
else
  chmod 600 "$APP_DIR/.env"
fi

cd "$APP_DIR"
if [[ $NEW_ENV == true || $FORCE_CONFIGURE == true ]]; then
  connection=(${SSH_CONNECTION:-})
  export SETUP_SSH_USER=${SETUP_SSH_USER:-${SUDO_USER:-$(id -un)}}
  export SETUP_SSH_HOST=${SETUP_SSH_HOST:-${connection[2]:-}}
  export SETUP_SSH_PORT=${SETUP_SSH_PORT:-${connection[3]:-22}}
  echo "Starting the configuration page. Installation continues after you save it."
  docker compose --profile setup run --rm --build --service-ports setup
fi

if [[ $START == true ]]; then
  docker compose up -d --build trendradar
  docker compose ps trendradar
  echo "Docker deployment completed. Logs: docker compose logs -f trendradar"
else
  echo "Configuration completed. Start later with: docker compose up -d --build trendradar"
fi

echo "Reconfigure: ./deploy/docker/install.sh --configure"
