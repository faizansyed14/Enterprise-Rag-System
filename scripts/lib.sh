#!/usr/bin/env bash
# Shared helpers for the scripts in this folder. Works with bash 3.2 (macOS) and later.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }

# docker compose from the project root; COMPOSE_FILE in .env (if set) adds docker-compose.dev.yml for local development.
# MSYS_NO_PATHCONV: Git Bash on Windows rewrites args like /data/demo into C:/Program Files/Git/data/demo.
compose() { (cd "$ROOT" && MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker compose "$@"); }

need_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is required: https://docs.docker.com/get-docker/"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (docker compose version)"
  docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker Desktop or the docker service."
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "${1:-24}"
  else
    python3 -c "import secrets, sys; print(secrets.token_hex(int(sys.argv[1])))" "${1:-24}"
  fi
}

# Value of KEY in .env (empty when missing).
env_get() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2-
}

# Set KEY=VALUE in .env, replacing an existing line or appending one.
env_set() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if grep -qE "^$key=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$value" 'BEGIN { FS = OFS = "=" } $1 == k { print k "=" v; next } { print }' "$ENV_FILE" >"$tmp"
  else
    cat "$ENV_FILE" >"$tmp"
    printf '%s=%s\n' "$key" "$value" >>"$tmp"
  fi
  cat "$tmp" >"$ENV_FILE"
  rm -f "$tmp"
}

# Create .env from .env.example on first run and fill every secret that is still empty or a placeholder.
GENERATED_ADMIN_PASSWORD=""
ensure_env() {
  GENERATED_ADMIN_PASSWORD=""
  if [ ! -f "$ENV_FILE" ]; then
    cp "$ROOT/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    bold "Created .env from .env.example"
  fi
  local secret
  case "$(env_get SECRET_KEY)" in "" | change-me*) env_set SECRET_KEY "$(random_secret 32)" ;; esac
  if [ -z "$(env_get POSTGRES_PASSWORD)" ]; then
    secret="$(random_secret 18)"
    env_set POSTGRES_PASSWORD "$secret"
    env_set DATABASE_URL "postgresql://$(env_get POSTGRES_USER):$secret@localhost:5432/$(env_get POSTGRES_DB)"
  fi
  [ -n "$(env_get QDRANT_API_KEY)" ] || env_set QDRANT_API_KEY "$(random_secret 24)"
  [ -n "$(env_get REDIS_PASSWORD)" ] || env_set REDIS_PASSWORD "$(random_secret 24)"
  [ -n "$(env_get LOCAL_MODELS_TOKEN)" ] || env_set LOCAL_MODELS_TOKEN "$(random_secret 24)"
  [ -n "$(env_get GRAFANA_ADMIN_PASSWORD)" ] || env_set GRAFANA_ADMIN_PASSWORD "$(random_secret 12)"
  if [ -n "$(env_get BOOTSTRAP_ADMIN_EMAIL)" ] && [ -z "$(env_get BOOTSTRAP_ADMIN_PASSWORD)" ]; then
    GENERATED_ADMIN_PASSWORD="$(random_secret 9)"
    env_set BOOTSTRAP_ADMIN_PASSWORD "$GENERATED_ADMIN_PASSWORD"
  fi
}

# The address the stack answers on, for messages.
site_url() {
  echo "http://localhost:$(env_get WEB_PORT || true)" | sed 's/:$/:3000/'
}

# Wait until the API answers its health check (inside the network, so it works before the proxy is up).
wait_for_api() {
  local tries="${1:-90}"
  printf '  waiting for the API'
  while [ "$tries" -gt 0 ]; do
    if compose exec -T backend python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=3).status == 200 else 1)" >/dev/null 2>&1; then
      printf ' ready\n'
      return 0
    fi
    printf '.'
    sleep 2
    tries=$((tries - 1))
  done
  printf '\n'
  return 1
}
