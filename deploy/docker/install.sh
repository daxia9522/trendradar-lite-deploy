#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
source "$SCRIPT_DIR/common.sh"
CONFIGURE=false
START=true
IMAGE_ACTION=auto
MODE=auto
for arg in "$@"; do
  case "$arg" in
    --configure) CONFIGURE=true ;;
    --no-start) START=false ;;
    --build) IMAGE_ACTION=build ;;
    --pull) IMAGE_ACTION=pull ;;
    --terminal) MODE=terminal ;;
    --web) MODE=web ;;
    *) fail "Usage: $0 [--configure] [--no-start] [--build|--pull] [--terminal|--web]" ;;
  esac
done
if [[ $CONFIGURE == true && $IMAGE_ACTION != auto ]]; then
  fail "--configure cannot pull/build/update images. Use update.sh explicitly."
fi
cd "$APP_DIR"
check_docker
set_identity

if [[ $CONFIGURE == true ]]; then
  require_local_setup_image
  # A failure/cancellation is final. Never retry setup with --build.
  run_configure "$MODE"
  echo "Configuration saved. No image update or service start/recreation was performed."
  exit 0
fi

check_launcher
if [[ $IMAGE_ACTION == auto && ! -f $APP_DIR/runtime/env ]]; then
  # Prefer the published image on first install, but require its runtime-config
  # compatibility label before executing setup. --build selects this checkout.
  IMAGE_ACTION=pull
fi
case $IMAGE_ACTION in
  build) docker compose build trendradar ;;
  pull) docker compose pull trendradar ;;
esac
require_local_setup_image
ensure_configuration "$MODE"
install_launcher
if [[ $START == true ]]; then
  run_manage init-volume
  docker compose up -d --no-build --pull never trendradar
  docker compose ps trendradar
  echo "Docker deployment completed. Logs: docker compose logs -f trendradar"
else
  echo "Configuration saved; no persistent service was started. Run install.sh to start later."
fi
echo "Reconfigure: ~/.local/bin/trendradar-docker"
echo "Update program/image separately: ./deploy/docker/update.sh [--pull|--build]"
