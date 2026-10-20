# rag-model-server

Serves the local models on ONNX Runtime (CPU by default; set `MODEL_PROVIDERS=CUDAExecutionProvider` with
`onnxruntime-gpu` for a GPU):

| Endpoint | Purpose |
|---|---|
| `POST /embed` | dense embeddings (`kind`: `query` or `passage`, model-specific prefixes applied here) |
| `POST /rerank` | cross-encoder relevance scores in [0, 1] |
| `POST /classify_pairs` | pair classifiers such as HHEM groundedness |
| `GET /models`, `GET /health` | what is loaded |

Download models once with `rag-model-server pull multilingual-e5-small ms-marco-MiniLM-L-6-v2` (into `MODELS_DIR`).
