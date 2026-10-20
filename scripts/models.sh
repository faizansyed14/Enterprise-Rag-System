#!/usr/bin/env bash
# Local models for the model server (stored in the "models" volume).
#   scripts/models.sh list
#   scripts/models.sh pull                         the models in LOCAL_MODELS
#   scripts/models.sh pull bge-m3 bge-reranker-v2-m3
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
ensure_env
compose up -d model-server >/dev/null
case "${1:-list}" in
  list) compose exec -T model-server rag-model-server list ;;
  pull)
    shift || true
    names="$*"
    [ -n "$names" ] || names="$(env_get LOCAL_MODELS | tr ',' ' ')"
    # shellcheck disable=SC2086
    compose exec -T model-server rag-model-server pull $names
    bold "Done. The model server loads them on first use; set EMBEDDER / RERANKER / LOCAL_MODELS in .env to use them."
    ;;
  *) die "Usage: scripts/models.sh list | pull [names...]" ;;
esac
