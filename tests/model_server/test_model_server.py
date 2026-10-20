"""The ONNX model server with real model files (RAG_TEST_MODELS_DIR: all-MiniLM-L6-v2 and ms-marco-MiniLM-L-6-v2).

Skipped when the folder is not there (models are downloaded with `rag-model-server pull`)."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import numpy as np
import pytest

MODELS = Path(os.environ.get("RAG_TEST_MODELS_DIR", "./models"))
EMBEDDER = "all-MiniLM-L6-v2"
RERANKER = "ms-marco-MiniLM-L-6-v2"
pytestmark = [
    pytest.mark.models,
    pytest.mark.skipif(not (MODELS / EMBEDDER / "model.onnx").is_file() or not (MODELS / RERANKER / "model.onnx").is_file(),
                       reason=f"set RAG_TEST_MODELS_DIR to a folder with {EMBEDDER} and {RERANKER}"),
]


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    monkeypatch.setenv("LOCAL_MODELS_TOKEN", "")
    from rag_model_server.app import create_app

    app = create_app(MODELS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ms") as c:
        yield c


async def _embed(client: httpx.AsyncClient, texts: list[str], kind: str = "passage") -> np.ndarray:
    response = await client.post("/embed", json={"model": EMBEDDER, "inputs": texts, "kind": kind})
    assert response.status_code == 200, response.text
    return np.array(response.json()["embeddings"])


async def test_embeddings_are_normalised_and_semantic(client: httpx.AsyncClient) -> None:
    vectors = await _embed(client, [
        "What concrete grade is used for the columns?",
        "Columns on Level 3 shall use concrete grade C40 with 20 mm aggregate.",
        "Provide access panels at each fire damper in the corridor ceiling.",
    ])
    assert vectors.shape == (3, 384)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)
    assert vectors[0] @ vectors[1] > vectors[0] @ vectors[2] + 0.15


async def test_batching_keeps_input_order(client: httpx.AsyncClient) -> None:
    texts = ["short", "a considerably longer sentence about reinforced concrete curing times", "mid length text here"]
    together = await _embed(client, texts)
    alone = np.vstack([await _embed(client, [t]) for t in texts])
    similarity = together @ alone.T
    # Each batched vector matches its own text (dynamic int8 quantisation allows tiny batch-dependent drift).
    assert list(similarity.argmax(axis=1)) == [0, 1, 2]
    assert np.all(np.diag(similarity) > 0.95)


async def test_reranker_prefers_the_answering_passage(client: httpx.AsyncClient) -> None:
    response = await client.post("/rerank", json={"model": RERANKER, "query": "how long must concrete be cured",
                                                  "documents": ["Keep exposed concrete surfaces moist for 7 days.",
                                                                "Fire dampers need access panels."]})
    scores = response.json()["scores"]
    assert 0 <= scores[1] < scores[0] <= 1


async def test_errors_are_clear(client: httpx.AsyncClient) -> None:
    assert (await client.post("/embed", json={"model": "nope", "inputs": ["x"]})).status_code == 404
    wrong = await client.post("/rerank", json={"model": EMBEDDER, "query": "q", "documents": ["d"]})
    assert wrong.status_code == 400 and "embedder" in wrong.json()["detail"]
    too_many = await client.post("/embed", json={"model": EMBEDDER, "inputs": ["x"] * 1000})
    assert too_many.status_code == 422


async def test_token_is_required_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_MODELS_TOKEN", "s3cret")
    from rag_model_server.app import create_app

    app = create_app(MODELS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ms") as c:
        denied = await c.post("/embed", json={"model": EMBEDDER, "inputs": ["x"]})
        allowed = await c.post("/embed", json={"model": EMBEDDER, "inputs": ["x"]},
                               headers={"Authorization": "Bearer s3cret"})
        health = await c.get("/health")
    assert denied.status_code == 401 and allowed.status_code == 200 and health.status_code == 200


async def test_tokenizer_is_served_for_exact_chunk_sizing(client: httpx.AsyncClient) -> None:
    ok = await client.get(f"/models/{EMBEDDER}/tokenizer.json")
    assert ok.status_code == 200 and "model" in ok.json()
    assert (await client.get("/models/..%2F..%2Fetc/tokenizer.json")).status_code == 404
