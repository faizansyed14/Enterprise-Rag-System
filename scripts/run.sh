#!/usr/bin/env bash
# One entry point for both environments. The first argument picks the compose files, whatever COMPOSE_FILE says in .env:
#
#   dev   docker-compose.yml + docker-compose.dev.yml  (small machine, source mounted, localhost ports)
#   prod  docker-compose.yml alone                      (large host: see "Production sizing" in .env.example)
#
#   scripts/run.sh dev|prod up [args]        build and start (extra args go to `docker compose up`, e.g. --scale worker=8)
#   scripts/run.sh dev|prod stop             stop, data kept
#   scripts/run.sh dev|prod down             remove containers, data volumes kept
#   scripts/run.sh dev|prod restart [svc..]  recreate (all application services, or the named ones)
#   scripts/run.sh dev|prod update           get the new code and apply it
#                                              dev : restart the Python services; rebuild the frontend image if the frontend changed
#                                              prod: git pull (if a repository), rebuild images, recreate; migrations run first
#   scripts/run.sh dev|prod status           containers, configuration check, queue
#   scripts/run.sh dev|prod logs [svc..]     follow logs
#   scripts/run.sh dev|prod check            validate the compose files and show the memory limits that apply
set -euo pipefail
source "$(dirname "$0")/lib.sh"

mode="${1:-}"
action="${2:-}"
[ "$#" -ge 2 ] || { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
shift 2

case "$mode" in
  dev)  export COMPOSE_PATH_SEPARATOR=":" COMPOSE_FILE="docker-compose.yml:docker-compose.dev.yml" ;;
  prod) export COMPOSE_PATH_SEPARATOR=":" COMPOSE_FILE="docker-compose.yml" ;;
  *) die "first argument must be dev or prod" ;;
esac

SCRIPTS="$(dirname "$0")"
bold "$mode ($COMPOSE_FILE)"

case "$action" in
  up)      exec "$SCRIPTS/start.sh" "$@" ;;
  stop)    need_docker; compose stop "$@" ;;
  down)    need_docker; compose down "$@" ;;
  restart) exec "$SCRIPTS/restart.sh" "$@" ;;
  status)  exec "$SCRIPTS/status.sh" ;;
  logs)    exec "$SCRIPTS/logs.sh" "$@" ;;
  check)
    need_docker
    compose config --quiet && note "compose files are valid"
    compose config | awk '
      /^[a-z]/ { in_services = ($0 == "services:"); next }
      !in_services { next }
      /^  [a-z][a-z0-9-]*:$/ { if (name != "") print_row(); name = substr($1, 1, length($1) - 1); mem = "none"; copies = 1; next }
      /^    mem_limit:/ { gsub(/"/, "", $2); mem = sprintf("%.1f GB", $2 / 1073741824) }
      /^      replicas:/ { copies = $2 }
      function print_row() { printf "%-16s %12s %8s\n", name, mem, copies }
      BEGIN { printf "%-16s %12s %8s\n", "service", "memory limit", "copies" }
      END { if (name != "") print_row() }'
    ;;
  update)
    need_docker
    ensure_env
    if [ "$mode" = "prod" ]; then
      if [ -d "$ROOT/.git" ]; then bold "Getting the new code"; (cd "$ROOT" && git pull --ff-only); fi
      bold "Building and recreating (migrations run first)"
      compose up -d --build --remove-orphans
    else
      before="$(cd "$ROOT" && find frontend/src frontend/package.json -type f -newer "$ENV_FILE" 2>/dev/null | head -n 1)"
      if [ -d "$ROOT/.git" ]; then bold "Getting the new code"; (cd "$ROOT" && git pull --ff-only) || warn "git pull skipped"; fi
      # the Python source is mounted into the containers: a restart is enough
      compose up -d --force-recreate --no-deps backend worker scheduler
      if [ -n "$before" ] || [ "${REBUILD_WEB:-}" = "1" ]; then
        bold "Frontend changed: rebuilding the web image"
        compose build frontend && compose up -d frontend
      else
        note "Frontend not rebuilt (set REBUILD_WEB=1 to force)."
      fi
    fi
    wait_for_api 120 || { warn "The API is not ready: scripts/run.sh $mode logs backend"; exit 1; }
    bold "Updated. $(site_url)"
    ;;
  *) die "unknown action '$action' (up, stop, down, restart, update, status, logs, check)" ;;
esac
