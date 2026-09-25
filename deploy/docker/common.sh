#!/usr/bin/env bash
# Shared host operations. Requires Bash, Docker Compose v2, ordinary coreutils;
# Python is deliberately only used inside the already-selected image.

fail() { printf '%s\n' "$*" >&2; exit 2; }

check_docker() {
  command -v docker >/dev/null || fail "Docker is required."
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required."
}

read_saved_id() {
  local key=$1 line value
  [[ -f $APP_DIR/.env ]] || return 0
  # Only read a strict numeric literal; never source/eval private .env contents.
  line=$(grep -E "^[[:space:]]*$key[[:space:]]*=" "$APP_DIR/.env" | tail -n 1) || true
  [[ -n $line ]] || return 0
  value=$(printf '%s\n' "$line" | sed -nE "s/^[[:space:]]*$key[[:space:]]*=[[:space:]]*[\"']?([0-9]+)[\"']?[[:space:]]*$/\\1/p")
  [[ -n $value ]] || fail "Stored TRENDRADAR_UID/GID must be explicit numeric literals."
  printf '%s\n' "$value"
}

set_identity() {
  local uid gid
  uid=$(read_saved_id TRENDRADAR_UID)
  gid=$(read_saved_id TRENDRADAR_GID)
  if [[ -z $uid || -z $gid ]]; then
    if [[ $(id -u) == 0 ]]; then
      uid=${SUDO_UID:-1000}
      gid=${SUDO_GID:-1000}
      [[ $uid != 0 ]] || uid=1000
      [[ $gid != 0 ]] || gid=1000
    else
      uid=$(id -u)
      gid=$(id -g)
    fi
  fi
  [[ $uid =~ ^[1-9][0-9]{0,9}$ && $gid =~ ^[1-9][0-9]{0,9}$ ]] || fail "TRENDRADAR_UID/GID must be non-root numeric IDs."
  (( uid <= 2147483647 && gid <= 2147483647 )) || fail "TRENDRADAR_UID/GID are outside the supported range."
  export TRENDRADAR_UID=$uid TRENDRADAR_GID=$gid
}

require_local_setup_image() {
  local image compatibility
  # Compose >= v2.24 accepts the setup service as a positional selector for
  # `config --images`. Older v2 releases reject or silently ignore it and
  # print one image per service, so fall back to the whole-project listing
  # deduplicated; every setup-profile service references one image variable.
  if image=$(docker compose --profile setup config --images setup 2>/dev/null) && [[ -n $image && $image != *$'\n'* ]]; then
    :
  else
    image=$(docker compose --profile setup config --images | sort -u) || fail "Cannot resolve the setup image."
  fi
  [[ -n $image && $image != *$'\n'* ]] || fail "Expected exactly one setup image."
  compatibility=$(docker image inspect --format '{{ index .Config.Labels "org.trendradar.runtime-config" }}' "$image" 2>/dev/null) || fail "Setup image is not available locally. Configuration never pulls/builds images; run install.sh --build or update.sh explicitly."
  [[ $compatibility == 1 ]] || fail "Local image does not support runtime configuration (org.trendradar.runtime-config=1). Build/install a compatible image explicitly; configuration will not update it."
}

# Compose 'run' has --pull never but no --no-build. The setup service intentionally
# has no build stanza; install/update explicitly build the trendradar image first.
run_manage() {
  if [[ ${1:-} == init-volume ]]; then
    # This service mounts only the output volume (no project/config mount and
    # no network). Ordinary setup/configuration never creates that volume.
    [[ $# == 1 ]] || fail "init-volume does not accept path overrides."
    docker compose --profile setup run --rm --pull never --no-deps -T \
      -e TRENDRADAR_UID -e TRENDRADAR_GID volume-init
    return
  fi
  docker compose --profile setup run --rm --pull never --no-deps -T \
    -e TRENDRADAR_UID -e TRENDRADAR_GID --entrypoint python setup \
    deploy/docker/manage.py "$@" --root /setup
}

run_configure() {
  local mode=${1:-auto}
  local -a options=(--rm --pull never --no-deps -e TRENDRADAR_UID -e TRENDRADAR_GID)
  if [[ $mode == auto ]]; then
    if [[ -t 0 && -t 1 ]]; then mode=terminal; else mode=web; fi
  fi
  if [[ $mode == web ]]; then options+=(--service-ports -T); fi
  connection=(${SSH_CONNECTION:-})
  export SETUP_SSH_USER=${SETUP_SSH_USER:-${SUDO_USER:-$(id -un)}}
  export SETUP_SSH_HOST=${SETUP_SSH_HOST:-${connection[2]:-}}
  export SETUP_SSH_PORT=${SETUP_SSH_PORT:-${connection[3]:-22}}
  docker compose --profile setup run "${options[@]}" setup --runtime-config --mode "$mode"
}

ensure_configuration() {
  # prepare only preflights paths. Old .env and example values stay in memory
  # until the same menu confirms them, whether installing or upgrading.
  run_manage prepare
  if [[ ! -f $APP_DIR/runtime/env ]]; then
    echo "Starting grouped Docker configuration. Legacy values/defaults remain a draft until confirmed."
    run_configure "${1:-auto}"
  elif ! run_manage check; then
    echo "Existing runtime configuration requires correction; opening the configuration menu."
    run_configure "${1:-auto}"
  fi
  [[ -f $APP_DIR/runtime/env ]] || fail "Configuration was not saved; services were not started."
  run_manage check
  # Official installation metadata is not written for a cancelled/invalid draft.
  run_manage persist-identity
}

launcher_path() { printf '%s/.local/bin/trendradar-docker' "$HOME"; }

check_launcher() {
  local launcher
  launcher=$(launcher_path)
  [[ ! -L $launcher ]] || fail "Refusing to replace a symlink at trendradar-docker."
  if [[ -e $launcher ]]; then
    [[ -f $launcher ]] || fail "Refusing to replace an unknown trendradar-docker entry."
    grep -qx '# Managed by TrendRadar Docker installer v1' "$launcher" || fail "Refusing to overwrite an unknown ~/.local/bin/trendradar-docker."
  fi
}

install_launcher() {
  local launcher temporary
  check_launcher
  launcher=$(launcher_path)
  mkdir -p -- "$(dirname -- "$launcher")"
  temporary=$(mktemp "${launcher}.XXXXXX")
  {
    printf '#!/usr/bin/env bash\n# Managed by TrendRadar Docker installer v1\n'
    printf 'exec bash %q --configure "$@"\n' "$APP_DIR/deploy/docker/install.sh"
  } > "$temporary"
  chmod 755 "$temporary"
  mv -f -- "$temporary" "$launcher"
  printf 'Independent configuration entry: %s (no shell startup files changed)\n' "$launcher"
}

remove_launcher() {
  local launcher
  launcher=$(launcher_path)
  if [[ -f $launcher && ! -L $launcher ]] && grep -qx '# Managed by TrendRadar Docker installer v1' "$launcher"; then
    local quoted
    printf -v quoted '%q' "$APP_DIR/deploy/docker/install.sh"
    if grep -Fqx "exec bash $quoted --configure \"\$@\"" "$launcher"; then
      rm -f -- "$launcher"
    fi
  fi
}
