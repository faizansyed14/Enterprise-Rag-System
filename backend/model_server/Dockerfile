# syntax=docker/dockerfile:1.7
# Local ONNX model server (embeddings, reranking, pair classification). Internal only: compose does not publish it.
# Build from backend/model_server/:  docker build -t rag-model-server .
# Models live in the /models volume; download them once with:
#   docker compose run --rm model-server rag-model-server pull multilingual-e5-small ms-marco-MiniLM-L-6-v2

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION=0.11.32
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 rag \
 && useradd --uid 10001 --gid rag --home-dir /home/rag --create-home --shell /usr/sbin/nologin rag \
 && mkdir -p /models && chown rag:rag /models
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MODELS_DIR=/models \
    HF_HOME=/models/.hf-cache \
    MODEL_HOST=0.0.0.0 \
    MODEL_PORT=8080 \
    HOME=/home/rag
WORKDIR /app
USER rag
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"]
CMD ["rag-model-server", "serve"]
