#!/usr/bin/env bash
# Stop every container (including the optional monitoring and ClamAV ones). Data stays in the Docker volumes.
#   scripts/stop.sh            stop
#   scripts/stop.sh --purge    stop and DELETE all data volumes (database, index, originals, models); asks first
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker

if [ "${1:-}" = "--purge" ]; then
  warn "This deletes the database, the search index, stored originals, downloaded models and monitoring data."
  printf 'Type "delete everything" to continue: '
  read -r answer
  [ "$answer" = "delete everything" ] || die "Nothing was deleted."
  compose --profile monitoring --profile clamav down --volumes --remove-orphans
  bold "Stopped and deleted all data volumes."
else
  compose --profile monitoring --profile clamav down --remove-orphans
  bold "Stopped. Data is kept; scripts/start.sh starts again."
fi
