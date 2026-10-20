"""Groundedness judges: is an answer sentence supported by the passage it cites?

  builtin/lexical          offline: content-word coverage + every number in the claim must appear in the passage
  local/hhem-2.1-open      Vectara HHEM on the model server (English); export it once with scripts/export_hhem.py
  <chat provider>/<model>  an LLM judge (works for Arabic): openrouter/anthropic/claude-haiku-5.5 etc.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.plugins.capabilities import ChatMessage
from rag.plugins.registry import registry
from rag.providers.local_models import LocalModelClient
from rag.text.bm25 import BM25Analyzer
from rag.text.normalize import normalize_for_search

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(normalize_for_search(text))}


def lexical_support(premise: str, hypothesis: str, analyzer: BM25Analyzer | None = None) -> float:
    analyzer = analyzer or BM25Analyzer()
    claim = [t for t in analyzer.tokens(hypothesis) if not t.startswith("#")]
    if not claim:
        return 1.0
    source = set(analyzer.tokens(premise))
    coverage = sum(1 for t in claim if t in source) / len(claim)
    claim_numbers = numbers_in(hypothesis)
    if claim_numbers:
        missing = claim_numbers - numbers_in(premise)
        if missing:
            # A number the passage does not contain is the classic hallucination; cap the score hard.
            coverage = min(coverage, 0.3)
    return round(coverage, 4)


class LexicalJudge:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self.analyzer = BM25Analyzer()

    async def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        return [lexical_support(p, h, self.analyzer) for p, h in pairs]

    async def aclose(self) -> None:
        return None


class LocalPairJudge:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self._client = LocalModelClient(settings)

    async def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        data = await self._client.post("/classify_pairs", {"model": self.spec.model,
                                                           "pairs": [[p, h] for p, h in pairs]})
        return [float(s) for s in data["scores"]]

    async def aclose(self) -> None:
        await self._client.aclose()


JUDGE_PROMPT = (
    "You check answers against sources. For each numbered pair, decide whether the CLAIM is fully supported by the "
    "SOURCE (same facts, numbers, names and conditions; paraphrase is fine). Reply with JSON only: "
    '{"scores": [number between 0 and 1 for each pair, in order]}.'
)


class LLMJudge:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        from rag.plugins.registry import registry as reg

        self.spec = spec
        chat_spec = RoleSpec(role="LLM_ROUTER", provider=spec.provider, model=spec.model, options=spec.options)
        info = reg.info(Capability.CHAT, spec.provider)
        if info is None:
            raise ValueError(f"no chat provider {spec.provider!r} for the groundedness judge")
        self._chat: Any = info.factory(chat_spec, settings)

    async def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        body = "\n\n".join(f"[{i + 1}] SOURCE: {p[:3000]}\nCLAIM: {h}" for i, (p, h) in enumerate(pairs))
        result = await self._chat.complete([ChatMessage("system", JUDGE_PROMPT), ChatMessage("user", body)],
                                           max_tokens=200, temperature=0.0, json_mode=True)
        try:
            text = result.content[result.content.find("{"): result.content.rfind("}") + 1]
            scores = [float(s) for s in json.loads(text)["scores"]]
        except (ValueError, KeyError, TypeError):
            return [lexical_support(p, h) for p, h in pairs]
        if len(scores) != len(pairs):
            return [lexical_support(p, h) for p, h in pairs]
        return [max(0.0, min(1.0, s)) for s in scores]

    async def aclose(self) -> None:
        await self._chat.aclose()


registry.register(Capability.GROUNDEDNESS, "builtin", local=True,
                  description="Offline content-word and number coverage")(LexicalJudge)
registry.register(Capability.GROUNDEDNESS, "local", requires=("local_models_url",), local=True,
                  description="HHEM-2.1-open on the local model server (English)")(LocalPairJudge)
for _provider, _keys in {"openrouter": ("openrouter_api_key",), "openai": ("openai_api_key",),
                         "anthropic": ("anthropic_api_key",), "azure_openai": ("azure_openai_api_key",),
                         "openai_compat": ("openai_compat_base_url",)}.items():
    registry.register(Capability.GROUNDEDNESS, _provider, requires=_keys,
                      description="LLM judge (handles Arabic)")(LLMJudge)
