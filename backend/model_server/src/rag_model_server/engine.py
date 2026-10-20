"""ONNX Runtime inference for encoder models: embeddings (mean/CLS pooling) and cross-encoders (pair scores)."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np

from rag_model_server.catalog import CATALOG, ModelSpec

log = logging.getLogger(__name__)

MAX_BATCH = int(os.environ.get("MODEL_MAX_BATCH", "32"))


def _session(path: Path) -> Any:
    import onnxruntime as ort

    options = ort.SessionOptions()
    threads = int(os.environ.get("MODEL_THREADS", "0"))
    if threads:
        options.intra_op_num_threads = threads
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    providers = [p.strip() for p in os.environ.get("MODEL_PROVIDERS", "CPUExecutionProvider").split(",") if p.strip()]
    available = set(ort.get_available_providers())
    providers = [p for p in providers if p in available] or ["CPUExecutionProvider"]
    return ort.InferenceSession(str(path), sess_options=options, providers=providers)


def _run_options() -> Any:
    """Per-request options that hand the CPU memory arena back after every run.

    Text inputs change length with every request. ONNX Runtime's CPU arena keeps the largest block it has ever needed and never
    returns it, so a long-lived server grows until the container's memory limit kills it (seen: a restart about every hour with
    three models loaded). Disabling the arena stops that but costs 27-30% of the speed; shrinking it after each run keeps full
    speed and lowers resident memory by 40-45% (measured on mmarco-mMiniLMv2-L12 and multilingual-e5-small).
    MODEL_ARENA_SHRINK=0 turns it off."""
    import onnxruntime as ort

    run_options = ort.RunOptions()
    if os.environ.get("MODEL_ARENA_SHRINK", "1") != "0":
        run_options.add_run_config_entry("memory.enable_memory_arena_shrinkage", "cpu:0")
    return run_options


def _tokenizer(path: Path, max_tokens: int) -> Any:
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(str(path))
    tok.enable_truncation(max_length=max_tokens)
    pad_id = tok.token_to_id("<pad>")
    pad_token = "<pad>"
    if pad_id is None:
        pad_id = tok.token_to_id("[PAD]") or 0
        pad_token = "[PAD]"
    tok.enable_padding(pad_id=pad_id, pad_token=pad_token)
    return tok


class LoadedModel:
    def __init__(self, spec: ModelSpec, folder: Path) -> None:
        self.spec = spec
        self.folder = folder
        model_file = folder / "model.onnx"
        tokenizer_file = folder / "tokenizer.json"
        if not model_file.is_file() or not tokenizer_file.is_file():
            raise FileNotFoundError(f"{spec.name}: expected model.onnx and tokenizer.json in {folder}")
        self.session = _session(model_file)
        self.run_options = _run_options()
        self.tokenizer = _tokenizer(tokenizer_file, spec.max_tokens)
        self.input_names = {i.name for i in self.session.get_inputs()}
        self.lock = threading.Lock()  # one batch at a time per model keeps CPU threads from oversubscribing
        self.dims = spec.dims

    def _feed(self, encodings: list[Any]) -> dict[str, np.ndarray]:
        feed = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
        return {k: v for k, v in feed.items() if k in self.input_names}

    # ---- embeddings ----------------------------------------------------------------------------------------------------
    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        prefix = self.spec.query_prefix if kind == "query" else self.spec.passage_prefix
        inputs = [prefix + (t or " ") for t in texts]
        # Sort by length so each batch pads to similar lengths, then restore the order.
        order = sorted(range(len(inputs)), key=lambda i: len(inputs[i]))
        results: dict[int, np.ndarray] = {}
        for start in range(0, len(order), MAX_BATCH):
            idx = order[start: start + MAX_BATCH]
            encodings = self.tokenizer.encode_batch([inputs[i] for i in idx])
            feed = self._feed(encodings)
            with self.lock:
                outputs = self.session.run(None, feed, self.run_options)
            hidden = outputs[0]
            if hidden.ndim == 3:
                if self.spec.pooling == "cls":
                    pooled = hidden[:, 0, :]
                else:
                    mask = feed["attention_mask"][..., None].astype(np.float32)
                    pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
            else:
                pooled = hidden
            if self.spec.normalize:
                pooled = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
            for row, i in enumerate(idx):
                results[i] = pooled[row].astype(np.float32)
        if not results:
            return np.zeros((0, self.dims or 0), dtype=np.float32)
        vectors = np.stack([results[i] for i in range(len(inputs))])
        self.dims = int(vectors.shape[1])
        return vectors

    # ---- pair scoring ----------------------------------------------------------------------------------------------------
    def score_pairs(self, pairs: list[tuple[str, str]]) -> np.ndarray:
        scores = np.zeros(len(pairs), dtype=np.float32)
        for start in range(0, len(pairs), MAX_BATCH):
            chunk = pairs[start: start + MAX_BATCH]
            if self.spec.template:
                texts = [self.spec.template.format(premise=p, hypothesis=h) for p, h in chunk]
                encodings = self.tokenizer.encode_batch(texts)
            else:
                encodings = self.tokenizer.encode_batch([(a, b) for a, b in chunk])
            feed = self._feed(encodings)
            with self.lock:
                logits = self.session.run(None, feed, self.run_options)[0]
            logits = np.asarray(logits, dtype=np.float32)
            if self.spec.output == "softmax1" and logits.ndim == 2 and logits.shape[1] >= 2:
                exp = np.exp(logits - logits.max(axis=1, keepdims=True))
                values = exp[:, 1] / exp.sum(axis=1)
            elif self.spec.output == "prob":
                values = logits.reshape(len(chunk), -1)[:, -1]
            else:
                values = 1.0 / (1.0 + np.exp(-logits.reshape(len(chunk), -1)[:, 0]))
            scores[start: start + len(chunk)] = values
        return scores


class ModelRegistry:
    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self._loaded: dict[str, LoadedModel] = {}
        self._lock = threading.Lock()

    def spec(self, name: str) -> ModelSpec:
        spec = CATALOG.get(name)
        if spec is None:
            # Unknown names still work when the folder exists: treat them as mean-pooled embedders.
            if (self.models_dir / name / "model.onnx").is_file():
                return ModelSpec(name, "embedder")
            raise KeyError(name)
        return spec

    def get(self, name: str) -> LoadedModel:
        with self._lock:
            model = self._loaded.get(name)
            if model is None:
                model = LoadedModel(self.spec(name), self.models_dir / name)
                self._loaded[name] = model
                log.info("loaded %s (%s)", name, model.spec.kind)
            return model

    def loaded(self) -> list[str]:
        return sorted(self._loaded)

    def available(self) -> list[str]:
        if not self.models_dir.is_dir():
            return []
        return sorted(p.name for p in self.models_dir.iterdir() if (p / "model.onnx").is_file())
