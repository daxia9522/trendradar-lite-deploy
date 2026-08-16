#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
METHOD=${1:-}

if [[ -z $METHOD ]]; then
  printf '%s\n' \
    "Choose a deployment method:" \
    "  1) Native Linux + systemd" \
    "  2) Docker Compose"
  read -r -p "Selection [1-2]: " METHOD
fi

case "$METHOD" in
  1|linux) exec "$ROOT_DIR/deploy/linux/install.sh" ;;
  2|docker) exec "$ROOT_DIR/deploy/docker/install.sh" ;;
  *)
    echo "Usage: $0 [linux|docker]" >&2
    exit 2
    ;;
esac
