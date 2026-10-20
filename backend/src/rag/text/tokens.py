"""Token counting for chunk sizing.

Chunks are sized in the embedder's own tokens. When the local model server is reachable it serves the model's
tokenizer.json and counts are exact; otherwise a calibrated estimate is used (XLM-R/BERT style sub-words: about
1.3 tokens per English word, more for long words, Arabic and digits).
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

_PIECES = re.compile(r"[^\W\d_]+|\d+|[^\w\s]", re.UNICODE)


def estimate_tokens(text: str) -> int:
    total = 0
    for piece in _PIECES.findall(text):
        if piece.isdigit():
            total += 1 + len(piece) // 3
        elif piece.isalpha():
            if "؀" <= piece[0] <= "ۿ":
                total += 1 + len(piece) // 4
            else:
                total += 1 + len(piece) // 7
        else:
            total += 1
    return total


class TokenCounter:
    def __init__(self, tokenizer_path: str | Path | None = None) -> None:
        self._tok = None
        if tokenizer_path and Path(tokenizer_path).is_file():
            try:
                from tokenizers import Tokenizer

                self._tok = Tokenizer.from_file(str(tokenizer_path))
                self._tok.no_truncation()
                self._tok.no_padding()
            except Exception:
                log.warning("could not load tokenizer %s; using estimates", tokenizer_path, exc_info=True)
                self._tok = None

    @property
    def exact(self) -> bool:
        return self._tok is not None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._tok is not None:
            return len(self._tok.encode(text, add_special_tokens=False).ids)
        return estimate_tokens(text)

    def count_many(self, texts: list[str]) -> list[int]:
        if self._tok is not None and texts:
            return [len(e.ids) for e in self._tok.encode_batch(texts, add_special_tokens=False)]
        return [self.count(t) for t in texts]


@lru_cache(maxsize=4)
def counter_for(tokenizer_path: str | None) -> TokenCounter:
    return TokenCounter(tokenizer_path)
