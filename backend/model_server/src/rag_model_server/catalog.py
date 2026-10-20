"""Models the server knows how to download and run.

Each entry lists Hugging Face sources in order of preference (the original publisher's ONNX export first, then a
community export of the same weights) and how to use the model: pooling, normalisation, prompt prefixes, how to read
the classifier output.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Source:
    repo: str
    onnx: str  # path of the ONNX file in the repo
    extra: tuple[str, ...] = ()  # e.g. external weights "onnx/model.onnx_data"
    tokenizer: str = "tokenizer.json"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str  # embedder | reranker | pair_classifier
    sources: tuple[Source, ...] = ()
    dims: int | None = None
    max_tokens: int = 512
    pooling: str = "mean"  # mean | cls
    normalize: bool = True
    query_prefix: str = ""
    passage_prefix: str = ""
    output: str = "sigmoid"  # reranker/classifier: sigmoid of logit 0, or softmax1 (probability of class 1), or prob
    template: str = ""  # pair classifiers: how premise and hypothesis are combined into one input
    languages: tuple[str, ...] | None = None  # None means multilingual
    licence: str = ""
    notes: str = ""
    labels: dict[str, str] = field(default_factory=dict)


CATALOG: dict[str, ModelSpec] = {
    "multilingual-e5-small": ModelSpec(
        "multilingual-e5-small", "embedder",
        (Source("intfloat/multilingual-e5-small", "onnx/model.onnx"),
         Source("Xenova/multilingual-e5-small", "onnx/model.onnx")),
        dims=384, max_tokens=512, pooling="mean", query_prefix="query: ", passage_prefix="passage: ",
        licence="MIT", notes="Default. ~100 languages including Arabic; fast on CPU."),
    "multilingual-e5-small-int8": ModelSpec(
        "multilingual-e5-small-int8", "embedder",
        (Source("Xenova/multilingual-e5-small", "onnx/model_quantized.onnx"),),
        dims=384, max_tokens=512, pooling="mean", query_prefix="query: ", passage_prefix="passage: ",
        licence="MIT", notes="The same model with 8-bit weights: about twice as fast on CPU for a fraction of a point of "
        "retrieval quality (vectors differ from the full-precision ones: a separate index)."),
    "multilingual-e5-base": ModelSpec(
        "multilingual-e5-base", "embedder",
        (Source("intfloat/multilingual-e5-base", "onnx/model.onnx"),
         Source("Xenova/multilingual-e5-base", "onnx/model.onnx")),
        dims=768, max_tokens=512, pooling="mean", query_prefix="query: ", passage_prefix="passage: ", licence="MIT"),
    "multilingual-e5-large": ModelSpec(
        "multilingual-e5-large", "embedder",
        (Source("intfloat/multilingual-e5-large", "onnx/model.onnx", ("onnx/model.onnx_data",)),
         Source("Xenova/multilingual-e5-large", "onnx/model.onnx")),
        dims=1024, max_tokens=512, pooling="mean", query_prefix="query: ", passage_prefix="passage: ", licence="MIT"),
    "bge-m3": ModelSpec(
        "bge-m3", "embedder",
        (Source("BAAI/bge-m3", "onnx/model.onnx", ("onnx/model.onnx_data",)),
         Source("Xenova/bge-m3", "onnx/model.onnx")),
        dims=1024, max_tokens=1024, pooling="cls", licence="MIT", notes="Higher quality, ~5x slower than e5-small."),
    "all-MiniLM-L6-v2": ModelSpec(
        "all-MiniLM-L6-v2", "embedder",
        (Source("sentence-transformers/all-MiniLM-L6-v2", "onnx/model.onnx"),
         Source("Xenova/all-MiniLM-L6-v2", "onnx/model.onnx")),
        dims=384, max_tokens=256, pooling="mean", languages=("en",), licence="Apache-2.0", notes="English only."),
    "bge-small-en-v1.5": ModelSpec(
        "bge-small-en-v1.5", "embedder",
        (Source("BAAI/bge-small-en-v1.5", "onnx/model.onnx"), Source("Xenova/bge-small-en-v1.5", "onnx/model.onnx")),
        dims=384, max_tokens=512, pooling="cls",
        query_prefix="Represent this sentence for searching relevant passages: ", languages=("en",), licence="MIT"),
    "ms-marco-MiniLM-L-6-v2": ModelSpec(
        "ms-marco-MiniLM-L-6-v2", "reranker",
        (Source("cross-encoder/ms-marco-MiniLM-L-6-v2", "onnx/model.onnx"),
         Source("Xenova/ms-marco-MiniLM-L-6-v2", "onnx/model.onnx")),
        max_tokens=512, output="sigmoid", languages=("en",), licence="Apache-2.0",
        notes="Default reranker. English only; Arabic questions skip reranking (RERANKER_LANGUAGES)."),
    "bge-reranker-v2-m3": ModelSpec(
        "bge-reranker-v2-m3", "reranker",
        (Source("BAAI/bge-reranker-v2-m3", "onnx/model.onnx", ("onnx/model.onnx_data",)),
         Source("onnx-community/bge-reranker-v2-m3-ONNX", "onnx/model.onnx", ("onnx/model.onnx_data",))),
        max_tokens=1024, output="sigmoid", licence="Apache-2.0", notes="Multilingual reranker; slower."),
    "mmarco-mMiniLMv2-L12": ModelSpec(
        "mmarco-mMiniLMv2-L12", "reranker",
        (Source("cross-encoder/mmarco-mMiniLMv2-L12-H384-v1", "onnx/model.onnx"),),
        max_tokens=512, output="sigmoid", licence="Apache-2.0",
        notes="Multilingual cross-encoder trained on mMARCO (Arabic, English and 12 more languages); small enough for CPU."),
    "mmarco-mMiniLMv2-L12-int8": ModelSpec(
        "mmarco-mMiniLMv2-L12-int8", "reranker",
        (Source("cross-encoder/mmarco-mMiniLMv2-L12-H384-v1", "onnx/model_quint8_avx2.onnx"),),
        max_tokens=512, output="sigmoid", licence="Apache-2.0", notes="8-bit weights of the model above."),
    "bge-reranker-v2-m3-int8": ModelSpec(
        "bge-reranker-v2-m3-int8", "reranker",
        (Source("onnx-community/bge-reranker-v2-m3-ONNX", "onnx/model_int8.onnx"),),
        max_tokens=1024, output="sigmoid", licence="Apache-2.0",
        notes="The strongest open multilingual reranker, 8-bit; about 25x the cost of the mMARCO model."),
    "hhem-2.1-open": ModelSpec(
        "hhem-2.1-open", "pair_classifier", (), max_tokens=1024, output="prob", languages=("en",),
        template="<pad> Determine if the hypothesis is true given the premise?\n\nPremise: {premise}\n\nHypothesis: {hypothesis}",
        licence="Apache-2.0", notes="Export once with scripts/export_hhem.py (needs torch); not downloadable as ONNX."),
}
