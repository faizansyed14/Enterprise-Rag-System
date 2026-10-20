"""HTTP API of the model server.

Security: binds to the internal network only (compose does not publish its port); set LOCAL_MODELS_TOKEN on both
sides to require a bearer token as well. Requests are bounded (inputs per call, characters per input) so one caller
cannot exhaust memory.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from rag_model_server import __version__
from rag_model_server.catalog import CATALOG
from rag_model_server.engine import ModelRegistry

log = logging.getLogger(__name__)
MAX_INPUTS = int(os.environ.get("MODEL_MAX_INPUTS", "256"))
MAX_CHARS = int(os.environ.get("MODEL_MAX_CHARS", "20000"))


class EmbedRequest(BaseModel):
    model: str
    inputs: list[str] = Field(max_length=MAX_INPUTS)
    kind: Literal["query", "passage"] = "passage"


class RerankRequest(BaseModel):
    model: str
    query: str = Field(max_length=MAX_CHARS)
    documents: list[str] = Field(max_length=MAX_INPUTS)


class PairsRequest(BaseModel):
    model: str
    pairs: list[tuple[str, str]] = Field(max_length=MAX_INPUTS)


def create_app(models_dir: Path | None = None) -> FastAPI:
    folder = models_dir or Path(os.environ.get("MODELS_DIR", "./models"))
    registry = ModelRegistry(folder)
    token = os.environ.get("LOCAL_MODELS_TOKEN", "")
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for name in [n.strip() for n in os.environ.get("LOCAL_MODELS", "").split(",") if n.strip()]:
            try:
                await asyncio.to_thread(registry.get, name)
            except (KeyError, FileNotFoundError) as exc:
                log.warning("model %s not loaded: %s", name, exc)
        yield

    app = FastAPI(title="rag-model-server", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.registry = registry

    async def auth(authorization: str = Header(default="")) -> None:
        if token and not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="invalid token")

    def model_or_404(name: str, kind: str):  # noqa: ANN202
        try:
            model = registry.get(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown model {name}") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if model.spec.kind != kind:
            raise HTTPException(status_code=400, detail=f"{name} is a {model.spec.kind}, not a {kind}")
        return model

    def clip(texts: list[str]) -> list[str]:
        return [t[:MAX_CHARS] for t in texts]

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__, "loaded": registry.loaded(), "available": registry.available()}

    @app.get("/models", dependencies=[Depends(auth)])
    async def models() -> dict:
        available = set(registry.available())
        rows = []
        for name in sorted(set(CATALOG) | available):
            spec = CATALOG.get(name)
            rows.append({"name": name, "kind": spec.kind if spec else "embedder", "dims": spec.dims if spec else None,
                         "max_tokens": spec.max_tokens if spec else 512,
                         "languages": list(spec.languages) if spec and spec.languages else None,
                         "downloaded": name in available, "loaded": name in registry.loaded(),
                         "notes": spec.notes if spec else ""})
        return {"models": rows}

    @app.get("/models/{name}/tokenizer.json", dependencies=[Depends(auth)])
    async def tokenizer(name: str) -> FileResponse:
        path = (folder / name / "tokenizer.json").resolve()
        if folder.resolve() not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="tokenizer not found")
        return FileResponse(path, media_type="application/json")

    @app.post("/embed", dependencies=[Depends(auth)])
    async def embed(req: EmbedRequest) -> dict:
        model = model_or_404(req.model, "embedder")
        start = time.perf_counter()
        vectors = await asyncio.to_thread(model.embed, clip(req.inputs), req.kind)
        return {"model": req.model, "dims": int(vectors.shape[1]) if len(vectors) else model.dims,
                "embeddings": vectors.round(6).tolist(), "ms": round((time.perf_counter() - start) * 1000, 1)}

    @app.post("/rerank", dependencies=[Depends(auth)])
    async def rerank(req: RerankRequest) -> dict:
        model = model_or_404(req.model, "reranker")
        scores = await asyncio.to_thread(model.score_pairs, [(req.query, d) for d in clip(req.documents)])
        return {"model": req.model, "scores": [round(float(s), 6) for s in scores]}

    @app.post("/classify_pairs", dependencies=[Depends(auth)])
    async def classify(req: PairsRequest) -> dict:
        model = model_or_404(req.model, "pair_classifier")
        pairs = [(p[:MAX_CHARS], h[:MAX_CHARS]) for p, h in req.pairs]
        scores = await asyncio.to_thread(model.score_pairs, pairs)
        return {"model": req.model, "scores": [round(float(s), 6) for s in scores]}

    return app
