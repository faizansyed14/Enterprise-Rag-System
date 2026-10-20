#!/usr/bin/env bash
# Create a local account.
#   scripts/admin.sh you@company.com             administrator (asks for a password)
#   scripts/admin.sh someone@company.com --user  ordinary user
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
[ -n "${1:-}" ] || die "Usage: scripts/admin.sh EMAIL [--user]"
role="--admin"
[ "${2:-}" = "--user" ] && role="--no-admin"
compose exec backend rag users create --email "$1" $role
