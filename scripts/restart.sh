#!/usr/bin/env bash
# Restart after changing .env (containers are recreated so they read the new values).
#   scripts/restart.sh             all application services
#   scripts/restart.sh backend worker  only these services
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
ensure_env
if [ "$#" -gt 0 ]; then
  compose up -d --force-recreate --no-deps "$@"
else
  compose up -d --force-recreate backend worker scheduler frontend model-server
fi
wait_for_api 90 || warn "The API is not ready yet: scripts/logs.sh backend"
bold "Restarted. $(site_url)"
