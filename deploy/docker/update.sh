#!/usr/bin/env bash
# Program/image upgrades are explicit and independent of saving configuration.
set -euo pipefail
umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
source "$SCRIPT_DIR/common.sh"
ACTION=pull
MODE=auto
for arg in "$@"; do
  case "$arg" in
    --pull) ACTION=pull ;;
    --build) ACTION=build ;;
    --terminal) MODE=terminal ;;
    --web) MODE=web ;;
    *) fail "Usage: $0 [--pull|--build] [--terminal|--web]" ;;
  esac
done
cd "$APP_DIR"
check_docker
set_identity
check_launcher
if [[ $ACTION == build ]]; then
  docker compose build trendradar
else
  docker compose pull trendradar
fi
require_local_setup_image
ensure_configuration "$MODE"
install_launcher
run_manage init-volume
docker compose up -d --no-build --pull never trendradar
docker compose ps trendradar
echo "Program/image update completed; existing valid runtime configuration was preserved, or missing/invalid configuration was confirmed in the menu."
echo "Reconfigure: ~/.local/bin/trendradar-docker"
