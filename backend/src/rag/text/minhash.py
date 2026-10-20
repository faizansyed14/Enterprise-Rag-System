"""MinHash signatures for near-duplicate detection (re-saved copies, a report filed in two folders, a cover page
added to an otherwise identical file).

Each version gets a 64-value signature over its word 3-gram shingles and 16 LSH band keys (4 values each). Two
versions whose shingle sets overlap by Jaccard >= 0.7 share at least one band key with ~99% probability, so an
indexed array-overlap query (GIN on `minhash_bands`) finds candidates without scanning; the signatures then estimate
the real overlap. Documents above NEAR_DUPLICATE_JACCARD join one family, and search shows one member per family.
"""

from __future__ import annotations

import re

import mmh3
import numpy as np

from rag.text.normalize import normalize_for_search

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
PERMUTATIONS = 64
BANDS = 16
ROWS = PERMUTATIONS // BANDS
MAX_WORDS = 50_000
NEAR_DUPLICATE_JACCARD = 0.75
_PRIME = np.uint64((1 << 31) - 1)
_rng = np.random.default_rng(20251015)
_A = _rng.integers(1, (1 << 31) - 1, size=PERMUTATIONS, dtype=np.uint64)
_B = _rng.integers(0, (1 << 31) - 1, size=PERMUTATIONS, dtype=np.uint64)


def shingles(text: str, size: int = 3) -> np.ndarray:
    words = _WORD.findall(normalize_for_search(text))[:MAX_WORDS]
    if len(words) < size:
        return np.zeros(0, dtype=np.uint64)
    hashes = {mmh3.hash(" ".join(words[i: i + size]), signed=False) for i in range(len(words) - size + 1)}
    return np.fromiter(hashes, dtype=np.uint64, count=len(hashes)) % _PRIME


def signature(text: str) -> list[int] | None:
    values = shingles(text)
    if values.size < 5:
        return None
    sig = np.full(PERMUTATIONS, np.iinfo(np.uint64).max, dtype=np.uint64)
    for start in range(0, values.size, 4096):
        part = values[start: start + 4096]
        hashed = (_A[:, None] * part[None, :] + _B[:, None]) % _PRIME
        sig = np.minimum(sig, hashed.min(axis=1))
    return [int(v) for v in sig]


def band_keys(sig: list[int]) -> list[int]:
    return [mmh3.hash(f"{b}:" + ",".join(str(v) for v in sig[b * ROWS:(b + 1) * ROWS]), signed=True)
            for b in range(BANDS)]


def similarity(a: list[int], b: list[int]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)
