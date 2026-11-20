#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
source "$SCRIPT_DIR/common.sh"
CONFIGURE=false
START=true
MODE=auto
for arg in "$@"; do
  case "$arg" in
    --configure) CONFIGURE=true ;;
    --no-start) START=false ;;
    --terminal) MODE=terminal ;;
    --web) MODE=web ;;
    *) fail "Usage: $0 [--configure] [--no-start] [--terminal|--web]" ;;
  esac
done
cd "$APP_DIR"
check_docker
set_identity

if [[ $CONFIGURE == true ]]; then
  require_local_setup_image
  # A failure/cancellation is final. Never retry by building an image.
  run_configure "$MODE"
  echo "Configuration saved. No image build/pull or service start/recreation was performed."
  exit 0
fi

check_launcher
# Installation/reinstallation is the only image build entry point.
docker build --tag "$APP_IMAGE" .
require_local_setup_image
ensure_configuration "$MODE"
install_launcher
run_manage init-volume
if [[ $START == true ]]; then
  docker compose up -d --no-build --pull never trendradar
  docker compose ps trendradar
  echo "Docker deployment completed. Logs: docker compose logs -f trendradar"
else
  echo "Configuration and data volume prepared; no persistent service was started."
  echo "Start later: docker compose up -d --no-build --pull never trendradar"
fi
echo "Reconfigure: ~/.local/bin/trendradar-docker"
echo "After updating source, rerun this installer to rebuild the local image."
