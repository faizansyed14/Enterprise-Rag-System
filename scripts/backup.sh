#!/usr/bin/env bash
# Back up what cannot be rebuilt: the database (users, conversations, metadata, jobs) and the stored originals.
# The search index can be rebuilt from them (rag index rebuild), so a Qdrant snapshot is optional (--with-index).
#   scripts/backup.sh [--with-index]          writes backups/<timestamp>/
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
stamp="$(date +%Y%m%d-%H%M%S)"
target="$ROOT/backups/$stamp"
mkdir -p "$target"

bold "Database"
compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6' >"$target/postgres.dump"
note "$(du -h "$target/postgres.dump" | cut -f1) postgres.dump"

bold "Originals and settings files"
compose exec -T backend tar -C /data -cf - originals eval >"$target/data.tar"
note "$(du -h "$target/data.tar" | cut -f1) data.tar"
cp "$ROOT/.env" "$target/env.backup"
chmod 600 "$target/env.backup"

if [ "${1:-}" = "--with-index" ]; then
  bold "Search index snapshot"
  key="$(env_get QDRANT_API_KEY)"
  alias="$(env_get QDRANT_ALIAS)"
  compose exec -T backend python - "$key" "${alias:-chunks_live}" >"$target/qdrant-snapshot.txt" <<'PY'
import json, sys, urllib.request
key, alias = sys.argv[1], sys.argv[2]
def call(method, path):
    req = urllib.request.Request(f"http://qdrant:6333{path}", method=method, headers={"api-key": key})
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.load(r)
aliases = call("GET", "/aliases")["result"]["aliases"]
collection = next(a["collection_name"] for a in aliases if a["alias_name"] == alias)
snap = call("POST", f"/collections/{collection}/snapshots?wait=true")["result"]["name"]
print(f"{collection} {snap}")
PY
  note "Snapshot $(cat "$target/qdrant-snapshot.txt") is in the qdrant_snapshots volume."
fi
bold "Backup written to backups/$stamp"
note "Keep a copy off this host. Restore with: scripts/restore.sh backups/$stamp"
