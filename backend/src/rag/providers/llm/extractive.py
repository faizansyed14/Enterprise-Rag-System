"""`extractive/default`: an offline answerer with no model behind it.

It reads the numbered evidence passages in the prompt, picks the sentences that best match the question and returns
them with their citation markers. It never writes anything that is not in a passage, so it is useful for tests,
demos without API keys and as a last-resort fallback. It does not call tools.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.plugins.capabilities import ChatMessage, ChatResult, StreamChunk, ToolSpec, Usage
from rag.plugins.registry import registry
from rag.text.bm25 import BM25Analyzer, overlap_score

_PASSAGE = re.compile(r"^\[(\d+)\][^\n]*\n(.*?)(?=^\[\d+\]|</evidence>|\Z)", re.S | re.M)
_QUESTION = re.compile(r"(?is)<question>\s*(.*?)\s*</question>")
# Sentence ends, line breaks, and labels that start a new statement ("... Engineer Response: Provide ...").
_SENTENCE = re.compile(r"(?<=[.!?؟])\s+|\n+|\s+(?=(?:Response|Answer|Question|Subject|Description|Note|Notes|"
                       r"Decision|Action|Comment|Comments|Reply)\s*:)")
_NUMERIC_QUESTION = re.compile(r"(?i)^\s*(what|which)\s+(?:[\w-]+\s+){0,2}(size|value|quantity|amount|percentage|"
                               r"grade|date|time|temperature|thickness|dimension|cover|length|strength|ratio|spacing|"
                               r"diameter|class)\b|^\s*how\s+(many|much|long|thick|big|deep|wide|high)\b|كم|متى")
# A value with a unit (600 x 600 mm, 50 mm, 0.40, 28 days, 25 MPa) or a concrete grade (C40, C32/40).
_MEASURE = re.compile(r"(?i)\b\d+(?:[.,]\d+)?\s*(?:x\s*\d+(?:[.,]\d+)?\s*)?(?:mm|cm|m|m2|m3|m²|m³|kg|t|kn|mpa|"
                      r"n/mm2|%|°c|days?|hours?|weeks?|months?)(?![\w/])|\bC\d{2}(?:/\d{2})?\b")
# The title block summary is written for search; it answers questions about the sheet itself, not its content.
_TITLE_BLOCK = re.compile(r"^Drawing sheet title block\b", re.I)
_SHEET_QUESTION = re.compile(r"(?i)\b(title|revision|rev|status|scale|date|issued?|drawn|checked|approved|sheet|"
                             r"drawing number|latest)\b")
_ARABIC = re.compile(r"[؀-ۿ]")
NOT_FOUND = "I could not find this in the documents you can access."
NOT_FOUND_AR = "لم أجد هذه المعلومة " \
               "في المستندات المتا" \
               "حة لك."
MIN_COVERAGE = 0.3


class ExtractiveChat:
    supports_tools = False
    supports_vision = False

    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self.settings = settings
        self.analyzer = BM25Analyzer()

    def _answer(self, messages: Sequence[ChatMessage]) -> str:
        prompt = "\n".join(m.content for m in messages if isinstance(m.content, str) and m.role in {"user", "system"})
        question_match = _QUESTION.search(prompt)
        question = question_match.group(1) if question_match else (
            next((m.content for m in reversed(messages) if m.role == "user" and isinstance(m.content, str)), ""))
        not_found = NOT_FOUND_AR if _ARABIC.search(question) else NOT_FOUND
        q_tokens = self.analyzer.tokens(question, query=True)
        identifiers = {t for t in q_tokens if t.startswith("#")}
        # Identifier tokens choose the passage; inside it they match the header, not the answer, so sentences are
        # scored on the rest of the question.
        id_parts = {part.lstrip("0") or "0" for t in identifiers for part in t[1:].split("-")}
        content = [t for t in q_tokens if not t.startswith("#") and (t.lstrip("0") or "0") not in id_parts]
        q_tokens = content or q_tokens
        numeric = bool(_NUMERIC_QUESTION.search(question))
        sheet_question = bool(_SHEET_QUESTION.search(question))
        scored: list[tuple[float, int, str]] = []
        for m in _PASSAGE.finditer(prompt):
            number = int(m.group(1))
            passage = m.group(2)
            if identifiers and not identifiers & set(self.analyzer.tokens(m.group(0))):
                continue  # the question names a document number this passage (label or text) does not contain
            for piece in _SENTENCE.split(passage):
                sentence = piece.strip()
                if len(sentence) < 12 or (_TITLE_BLOCK.match(sentence) and not sheet_question):
                    continue
                score = overlap_score(q_tokens, self.analyzer.tokens(sentence))
                if score <= 0:
                    continue
                if numeric:
                    score += 0.15 if _MEASURE.search(sentence) else 0.05 if re.search(r"\d", sentence) else 0.0
                scored.append((score - (number - 1) * 0.03, number, sentence))  # better-ranked passages win ties
        if not scored:
            return not_found
        scored.sort(reverse=True)
        best = scored[0][0]
        if best < MIN_COVERAGE:
            return not_found
        picked: list[tuple[int, str]] = []
        for score, number, sentence in scored:
            # A second sentence only when it is nearly as good: it should add to the answer, not dilute it.
            if score < best * 0.9 or len(picked) >= 2:
                break
            if all(sentence != s for _, s in picked):
                picked.append((number, sentence))
        return " ".join(f"{s} [{n}]" for n, s in picked) or not_found

    async def complete(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] | None = None,
                       max_tokens: int | None = None, temperature: float | None = None, json_mode: bool = False,
                       timeout: float | None = None) -> ChatResult:
        text = "{}" if json_mode else self._answer(messages)
        return ChatResult(content=text, usage=Usage(cost_usd=0.0), model="extractive", finish_reason="stop")

    async def stream(self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None,
                     temperature: float | None = None, timeout: float | None = None) -> AsyncIterator[StreamChunk]:
        text = self._answer(messages)
        for word in re.findall(r"\S+\s*", text):
            yield StreamChunk(delta=word)
            await asyncio.sleep(0)
        yield StreamChunk(result=ChatResult(content=text, usage=Usage(cost_usd=0.0), model="extractive"))

    async def aclose(self) -> None:
        return None


registry.register(Capability.CHAT, "extractive", local=True,
                  description="Offline answers built only from retrieved sentences (no model; tests and demos)")(
    ExtractiveChat)
