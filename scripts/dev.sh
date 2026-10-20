#!/usr/bin/env bash
# Development from source with hot reload: Postgres, Qdrant and the model server run in Docker (ports on localhost),
# the API, a worker and the web app run here. Needs uv (https://docs.astral.sh/uv/) and Node 22.
#   scripts/dev.sh            start everything (Ctrl+C stops the local processes)
#   scripts/dev.sh infra      only the Docker services
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
ensure_env
command -v uv >/dev/null 2>&1 || die "uv is required: https://docs.astral.sh/uv/getting-started/installation/"

(cd "$ROOT" && docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres qdrant model-server)
[ "${1:-}" = "infra" ] && { bold "Postgres :5432, Qdrant :6333 and the model server :8080 are up on localhost."; exit 0; }

# Same .env as Docker, with the addresses and folders of a local run.
export RAG_ENV_FILE="$ENV_FILE" LOCAL_MODELS_URL=http://localhost:8080 QDRANT_URL=http://localhost:6333 \
  OBJECT_STORE_URL="file://$ROOT/data/originals" CACHE_DIR="$ROOT/data/cache" EVAL_DIR="$ROOT/data/eval" \
  ALLOWED_ORIGINS="http://localhost:3000" PUBLIC_URL="http://localhost:3000"
mkdir -p "$ROOT/data/originals" "$ROOT/data/cache" "$ROOT/data/eval"
cd "$ROOT/backend"
uv sync --extra ocr --extra s3 --extra msg --extra demo
for _ in $(seq 1 30); do uv run rag db upgrade >/dev/null 2>&1 && break; sleep 2; done
uv run rag db upgrade

pids=""
cleanup() { [ -n "$pids" ] && kill $pids 2>/dev/null; exit 0; }
trap cleanup INT TERM EXIT
uv run rag api --reload & pids="$pids $!"
uv run rag worker & pids="$pids $!"
uv run rag scheduler & pids="$pids $!"
cd "$ROOT/frontend"
[ -d node_modules ] || npm ci
API_URL=http://localhost:8000 NEXT_PUBLIC_APP_NAME="$(env_get APP_NAME)" npm run dev & pids="$pids $!"
bold "Web http://localhost:3000   API http://localhost:8000   (Ctrl+C to stop)"
wait
