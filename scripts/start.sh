#!/usr/bin/env bash
# Build and start the whole stack. First run: creates .env with random secrets, downloads the local models and
# prints the administrator's sign-in. Extra arguments go to `docker compose up` (for example: --scale worker=3).
set -euo pipefail
source "$(dirname "$0")/lib.sh"

need_docker
ensure_env

bold "Building and starting ALAIN"
compose up -d --build --remove-orphans "$@"

if ! wait_for_api 120; then
  warn "The API did not become ready. Recent API logs:"
  compose logs --tail 40 backend || true
  exit 1
fi

# Local models: download the ones LOCAL_MODELS lists if the model server does not have them yet.
models="$(env_get LOCAL_MODELS | tr ',' ' ')"
if [ -n "$models" ]; then
  missing=""
  for name in $models; do
    if ! compose exec -T model-server sh -c "test -f /models/$name/model.onnx" >/dev/null 2>&1; then
      missing="$missing $name"
    fi
  done
  if [ -n "$missing" ]; then
    bold "Downloading local models:$missing (first run only)"
    # shellcheck disable=SC2086
    compose exec -T model-server rag-model-server pull $missing || warn "Model download failed; run scripts/models.sh pull later."
  fi
fi

bold "Configuration check"
compose exec -T backend rag check || warn "Some checks failed; see above. The Models and Monitor pages show the same."

url="$(site_url)"
bold "ALAIN is running at $url"
if [ -n "$GENERATED_ADMIN_PASSWORD" ]; then
  note "First administrator: $(env_get BOOTSTRAP_ADMIN_EMAIL)"
  note "Password:            $GENERATED_ADMIN_PASSWORD"
  note "(also in .env as BOOTSTRAP_ADMIN_PASSWORD; change it after signing in and clear it from .env)"
fi
case "$(env_get LLM_ANSWER)" in
  openrouter/*) [ -n "$(env_get OPENROUTER_API_KEY)" ] || warn "OPENROUTER_API_KEY is empty: add it to .env (or use the offline preset at the end of .env), then scripts/restart.sh" ;;
  anthropic/*) [ -n "$(env_get ANTHROPIC_API_KEY)" ] || warn "ANTHROPIC_API_KEY is empty: add it to .env, then scripts/restart.sh" ;;
  openai/*) [ -n "$(env_get OPENAI_API_KEY)" ] || warn "OPENAI_API_KEY is empty: add it to .env, then scripts/restart.sh" ;;
esac
note "Sample library and demo users: scripts/demo.sh    Monitoring with Grafana: scripts/monitoring.sh start"
