#!/usr/bin/env bash
# Containers, their health, and the application's own configuration check.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
compose --profile monitoring --profile clamav ps
echo
compose exec -T backend rag check 2>/dev/null || warn "The API container is not running."
echo
compose exec -T backend rag jobs stats 2>/dev/null || true
