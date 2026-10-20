"""Model prices for providers that do not report cost themselves (OpenRouter does; OpenAI and Anthropic do not).

MODEL_PRICES_FILE points at a YAML file with USD per million tokens:

    anthropic/claude-haiku-5-5: {input: 1.00, output: 5.00, cached_input: 0.10}
    openai/gpt-5-mini:          {input: 0.25, output: 2.00}

Keys are role lines without the '@dims' or '?options' parts. Unknown models record tokens with no cost.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from rag.plugins.capabilities import Usage


@lru_cache(maxsize=4)
def load_prices(path: str) -> dict[str, dict[str, float]]:
    if not path or not Path(path).is_file():
        return {}
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(k): {kk: float(vv) for kk, vv in (v or {}).items()} for k, v in data.items()}


def cost_for(label: str, usage: Usage, prices_file: str) -> float | None:
    price = load_prices(prices_file).get(label)
    if not price:
        return None
    cached = usage.cached_input_tokens
    fresh = max(0, usage.input_tokens - cached)
    total = fresh * price.get("input", 0.0) + usage.output_tokens * price.get("output", 0.0)
    total += cached * price.get("cached_input", price.get("input", 0.0))
    return round(total / 1_000_000, 8)
