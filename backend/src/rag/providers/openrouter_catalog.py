"""The models OpenRouter offers, from its public catalogue (no key, no cost), cached for an hour.

Used by the chat box so that a person can pick any model OpenRouter serves instead of only the shortlist in MODEL_CHOICES.
A failure to reach the catalogue never breaks chat: the list is then empty (or the last good copy) and the shortlist still works.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

CATALOG_URL = "https://openrouter.ai/api/v1/models"
TTL_S = 3600
_cache: dict[str, Any] = {"at": 0.0, "models": []}


def _per_million(price: Any) -> float | None:
    try:
        return round(float(price) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


# Catalogue entries that are not models to answer or read pages with: music, speech and safety classifiers, and routers that
# pick a model for you (so the price and the privacy of a request cannot be known in advance).
_NOT_FOR_READING = (
    "lyria",
    "content-safety",
    "guard",
    "embed",
    "tts",
    "whisper",
    "moderation",
    "openrouter/",
    "-router",
    "/auto",
)


def _entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    model_id = str(raw.get("id") or "")
    if "/" not in model_id or any(word in model_id.lower() for word in _NOT_FOR_READING):
        return None
    architecture = raw.get("architecture") or {}
    outputs = architecture.get("output_modalities") or ["text"]
    if "text" not in outputs:
        return None  # image generators and the like cannot write an answer
    pricing = raw.get("pricing") or {}
    prompt, completion = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
    return {
        "id": model_id,
        "name": str(raw.get("name") or model_id),
        "context": raw.get("context_length"),
        "prompt_per_m": prompt,
        "completion_per_m": completion,
        "free": prompt == 0 and completion == 0,
        "vision": "image" in (architecture.get("input_modalities") or []),
    }


def parse(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [e for e in (_entry(r) for r in payload.get("data") or []) if e]
    return sorted(entries, key=lambda e: (e["id"].split("/")[0], e["name"].lower()))


async def models(*, force: bool = False) -> list[dict[str, Any]]:
    if not force and _cache["models"] and time.monotonic() - _cache["at"] < TTL_S:
        return _cache["models"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(CATALOG_URL)
            response.raise_for_status()
            _cache["models"] = parse(response.json())
            _cache["at"] = time.monotonic()
    except Exception as exc:  # noqa: BLE001 - the shortlist keeps working without the catalogue
        log.warning("OpenRouter catalogue unavailable: %s", type(exc).__name__)
    return _cache["models"]


def allows(model_id: str, spec: str, *, free: bool | None = None) -> bool:
    """Is this model in the list `spec` (OPENROUTER_MODELS)? `free` is the model's catalogue flag when known; without it a
    ":free" id counts as free."""
    items = [i.strip() for i in spec.split(",") if i.strip()]
    if "*" in items or model_id in items:
        return True
    is_free = free if free is not None else model_id.endswith(":free")
    return "free" in items and is_free


def curate(models: list[dict[str, Any]], spec: str) -> list[dict[str, Any]]:
    return [m for m in models if allows(m["id"], spec, free=m["free"])]


def known_ids() -> set[str]:
    """Ids from the last good download (empty when none yet)."""
    return {m["id"] for m in _cache["models"]}
