#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh into a running stack, then rebuild the search index from it.
#   scripts/restore.sh backups/20260101-120000
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
source_dir="${1:-}"
[ -n "$source_dir" ] && [ -f "$source_dir/postgres.dump" ] || die "Usage: scripts/restore.sh backups/<timestamp>"
warn "This replaces the current database and stored originals with the backup."
printf 'Type "restore" to continue: '
read -r answer
[ "$answer" = "restore" ] || die "Nothing was changed."

compose stop backend worker scheduler frontend
compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' <"$source_dir/postgres.dump"
compose run --rm --no-deps -T --entrypoint "" backend tar -C /data -xf - <"$source_dir/data.tar"
compose up -d
wait_for_api 120 || die "The API did not come back: scripts/logs.sh backend"
compose exec -T backend rag index rebuild
bold "Restored. The index is being rebuilt in the background (Models page > Search index)."
