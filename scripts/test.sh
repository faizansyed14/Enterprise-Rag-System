#!/usr/bin/env bash
# All checks: backend lint and tests (unit + integration against throwaway Postgres and Qdrant containers) and the
# frontend typecheck, lint and production build.
#   scripts/test.sh                 everything
#   scripts/test.sh backend [args]  backend only; extra args go to pytest (for example -k retrieval)
#   scripts/test.sh frontend        frontend only
# Set RAG_TEST_DATABASE_URL and RAG_TEST_QDRANT_URL to use services you already run instead of Docker.
# Model-server tests need real models: RAG_TEST_MODELS_DIR=/path/with/all-MiniLM-L6-v2 (they skip otherwise).
set -euo pipefail
source "$(dirname "$0")/lib.sh"

part="${1:-all}"
[ "$#" -gt 0 ] && shift

run_backend() {
  command -v uv >/dev/null 2>&1 || die "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
  if [ -z "${RAG_TEST_DATABASE_URL:-}" ]; then
    need_docker
    bold "Starting throwaway Postgres and Qdrant for the tests"
    (cd "$ROOT" && docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile test up -d --wait test-postgres test-qdrant)
    trap '(cd "$ROOT" && docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile test rm -sfv test-postgres test-qdrant >/dev/null 2>&1)' EXIT
    export RAG_TEST_DATABASE_URL="postgresql://rag:rag@localhost:55432/rag_test"
    export RAG_TEST_QDRANT_URL="http://localhost:56333"
  fi
  cd "$ROOT/backend"
  uv sync --extra ocr --extra s3 --extra msg --extra demo
  bold "Lint (ruff)"
  uv run ruff check src ../tests
  bold "Security lint (bandit)"
  uv run bandit -q -c pyproject.toml -r src
  bold "Tests (pytest)"
  uv run pytest ../tests "$@"
}

run_frontend() {
  cd "$ROOT/frontend"
  [ -d node_modules ] || npm ci --no-audit --no-fund
  bold "Typecheck"
  npm run typecheck
  bold "Lint"
  npm run lint
  bold "Production build"
  npm run build
}

case "$part" in
  backend) run_backend "$@" ;;
  frontend) run_frontend ;;
  all) (run_backend "$@") && (run_frontend) ;;
  *) die "Usage: scripts/test.sh [all|backend|frontend] [pytest args]" ;;
esac
bold "All checks passed."
