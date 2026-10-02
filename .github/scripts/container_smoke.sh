#!/usr/bin/env bash
# Run the built image's public entrypoints without network or host credentials.
set -euo pipefail
image=${1:?Usage: container_smoke.sh IMAGE}
state=$(mktemp -d)
cleanup() {
  if [[ -s "$state/id" ]]; then
    read -r id < "$state/id" || true
    docker rm --force "$id" >/dev/null 2>&1 || true
  fi
  rm -f "$state/id"
  rmdir "$state"
}
trap cleanup EXIT

run() {
  timeout 60s docker run --cidfile "$state/id" --rm --network none \
    --cap-drop ALL --security-opt no-new-privileges:true \
    "$image" "$@"
  rm -f "$state/id"
}

run config-check
run doctor
run current --help
run weekly --help
run show-schedule
