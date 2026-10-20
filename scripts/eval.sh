#!/usr/bin/env bash
# Run a golden question set against the live index and models and print the scores.
#   scripts/eval.sh                  the demo set
#   scripts/eval.sh my-set           /data/eval/my-set.yaml (see docs/evaluation.md)
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
compose exec -T backend rag eval run --set "${1:-demo}"
