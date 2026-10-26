# syntax=docker/dockerfile:1.7
# One image for the API, the workers and the scheduler; the command picks the role:
#   rag api | rag worker --lanes control | rag worker --lanes interactive,bulk | rag scheduler | rag db upgrade
# Build from backend/:  docker build -t rag-backend .

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---- build: resolve the locked dependencies into /app/.venv ------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION=0.11.32
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first so this layer is cached until uv.lock changes.
COPY pyproject.toml uv.lock README.md ./
COPY model_server ./model_server
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra ocr --extra s3 --extra msg --extra demo
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra ocr --extra s3 --extra msg --extra demo

# ---- LibreDWG: reads AutoCAD .dwg files (rag.ingestion.cad) ----------------------------------------------------------------
# GNU LibreDWG (GPLv3, used as a separate program) is not packaged for Debian, so it is built here; only dwg2dxf and its library
# go into the image. The download is checked against a pinned SHA-256. One compile job at a time: a single source file needs
# more than 1 GB of memory to compile, so parallel jobs can exhaust a small Docker VM (raise LIBREDWG_JOBS on a larger one).
FROM ${PYTHON_IMAGE} AS libredwg
ARG LIBREDWG_VERSION=0.14
ARG LIBREDWG_SHA256=62ebb73b984f865960f20ed26619ea5f8789d5e3fd088fa40a2598384da81275
ARG LIBREDWG_JOBS=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config curl ca-certificates xz-utils \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/libredwg.tar.xz "https://ftp.gnu.org/gnu/libredwg/libredwg-${LIBREDWG_VERSION}.tar.xz" \
 && echo "${LIBREDWG_SHA256}  /tmp/libredwg.tar.xz" | sha256sum -c - \
 && tar -xJf /tmp/libredwg.tar.xz -C /tmp \
 && cd "/tmp/libredwg-${LIBREDWG_VERSION}" \
 && ./configure --prefix=/opt/libredwg --disable-bindings --disable-docs --disable-static >/dev/null \
 && make -j"${LIBREDWG_JOBS}" >/dev/null \
 && make install >/dev/null \
 && /opt/libredwg/bin/dwg2dxf --version

# ---- runtime -------------------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
# LibreOffice converts legacy .doc/.xls/.ppt/.rtf before parsing. It adds ~600 MB, so it is opt-in:
#   docker build --build-arg INSTALL_LIBREOFFICE=true .
ARG INSTALL_LIBREOFFICE=false
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libgomp1 libcairo2 fonts-dejavu-core \
 && if [ "$INSTALL_LIBREOFFICE" = "true" ]; then \
      apt-get install -y --no-install-recommends libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui; \
    fi \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 rag \
 && useradd --uid 10001 --gid rag --home-dir /home/rag --create-home --shell /usr/sbin/nologin rag \
 && mkdir -p /data/originals /data/cache /data/demo /data/eval \
 && chown -R rag:rag /data
COPY --from=build /app/.venv /app/.venv
# AutoCAD .dwg -> .dxf (libcairo2 and fonts-dejavu above draw the sheets for previews and the vision model)
COPY --from=libredwg /opt/libredwg/bin/dwg2dxf /usr/local/bin/dwg2dxf
COPY --from=libredwg /opt/libredwg/lib/libredwg.so* /usr/local/lib/
RUN ldconfig && dwg2dxf --version
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RAG_MIGRATIONS_DIR=/app \
    OBJECT_STORE_URL=file:///data/originals \
    CACHE_DIR=/data/cache \
    EVAL_DIR=/data/eval \
    HOME=/home/rag
WORKDIR /app
USER rag
EXPOSE 8000
# The API answers /api/v1/health; workers and the scheduler report heartbeats to the database instead (compose turns
# this check off for them).
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"]
CMD ["rag", "api"]
