"""Query understanding: what kind of question this is, what it refers to, and how to search for it.

Rules run first and always (they cost nothing and never fail). When LLM_ROUTER is set, one small JSON call refines
the plan within LLM_ROUTER_TIMEOUT_S: it rewrites follow-ups into standalone questions, translates non-English
questions to English for the English-only reranker and lexical matching, and flags questions that need the
research loop. If the call is slow or fails, the rules' plan is used unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from rag.metadata.identifiers import extract_identifiers
from rag.metadata.vocabularies import doc_type_from_text
from rag.plugins.capabilities import ChatMessage
from rag.retrieval.types import QueryPlan, SearchFilters
from rag.text.language import detect_language

log = logging.getLogger(__name__)

_HARD = re.compile(
    r"(?i)\b(compare|comparison|difference|differences|differ|versus|vs\.?|changed|change between|list (?:all|every)|"
    r"all the|every|summari[sz]e|overview of|which documents|across|between .* and)\b"
    r"|قارن|الفرق|جميع|كل\s+ال|ملخص")
_FOLLOW_UP = re.compile(r"(?i)^(and|also|what about|how about|same for|then|so|but)\b|\b(it|its|that|those|these|"
                        r"they|them|this one|the same|above)\b|^(و|وماذا|ماذا عن)")

ROUTER_PROMPT = """You prepare search queries for a construction document library.
Given the conversation and the latest question, return JSON only:
{"standalone": the latest question rewritten to be understandable on its own (same language),
 "english": an English translation of the standalone question (or the same text if already English),
 "complexity": "simple" or "hard" (hard = comparisons, multi-part, lists across many documents)}
Do not answer the question."""


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text, re.UNICODE)


class QueryRouter:
    def __init__(self, *, known_projects: list[str] | None = None, router_model: Any = None,
                 timeout_s: float = 3.0, reranker_languages: set[str] | None = None) -> None:
        self.known_projects = sorted(known_projects or [], key=len, reverse=True)
        self.router_model = router_model
        self.timeout_s = timeout_s
        self.reranker_languages = reranker_languages

    def rules(self, question: str, filters: SearchFilters | None = None,
              history: list[dict[str, str]] | None = None) -> QueryPlan:
        filters = filters or SearchFilters()
        text = question.strip()
        reasons: list[str] = []
        query = text
        previous = next((h["content"] for h in reversed(history or []) if h.get("role") == "user"), None)
        if previous and (len(_words(text)) <= 6 or _FOLLOW_UP.search(text)):
            query = f"{previous} {text}"
            reasons.append("follow-up: combined with the previous question")

        language = detect_language(text, min_letters=4)
        if language == "und":
            language = "en"
        identifiers = [i.key for i in extract_identifiers(query, query=True)]
        words = _words(text)
        if identifiers and len(words) <= 5:
            query_type = "identifier"
        elif len(words) <= 3:
            query_type = "keyword"
        elif language not in {"en", "latin"}:
            query_type = "non_english"
        else:
            query_type = "question"
        reasons.append(f"type: {query_type}")
        if identifiers:
            reasons.append("identifiers: " + ", ".join(identifiers))

        if not filters.project_ids and self.known_projects:
            lowered = text.lower()
            for project in self.known_projects:
                if re.search(rf"(?<!\w){re.escape(project.lower())}(?!\w)", lowered):
                    filters.project_ids = [project]
                    reasons.append(f"project: {project}")
                    break

        boosts: dict[str, list[str]] = {}
        if doc_type := doc_type_from_text(text):
            boosts["doc_types"] = [doc_type]
        if boosts:
            reasons.append("prefers: " + ", ".join(v[0] for v in boosts.values()))

        hard = bool(_HARD.search(text)) or text.count("?") > 1
        if hard:
            reasons.append("hard question: research loop and the stronger model")
        return QueryPlan(original=text, query=query, language=language, identifiers=identifiers,
                         query_type=query_type, filters=filters, hard=hard, needs_research=hard, boosts=boosts,
                         reasons=reasons)

    async def plan(self, question: str, filters: SearchFilters | None = None,
                   history: list[dict[str, str]] | None = None) -> QueryPlan:
        plan = self.rules(question, filters, history)
        wants_llm = self.router_model is not None and (
            (history and plan.query != plan.original)
            or (plan.language not in {"en", "latin"} and self.reranker_languages is not None
                and plan.language not in self.reranker_languages)
        )
        if not wants_llm:
            return plan
        try:
            refined = await asyncio.wait_for(self._llm(question, history or []), timeout=self.timeout_s)
        except (TimeoutError, Exception) as exc:  # noqa: BLE001 - the rules' plan is always a valid fallback
            plan.reasons.append(f"router model skipped ({type(exc).__name__})")
            return plan
        standalone = (refined.get("standalone") or "").strip()
        if standalone:
            plan.query = standalone
            plan.identifiers = sorted(set(plan.identifiers) | {i.key for i in extract_identifiers(standalone,
                                                                                                   query=True)})
        english = (refined.get("english") or "").strip()
        if english and plan.language not in {"en", "latin"}:
            plan.english = english
            plan.reasons.append("translated to English for reranking")
        if refined.get("complexity") == "hard" and not plan.hard:
            plan.hard = plan.needs_research = True
            plan.reasons.append("router model: hard question")
        plan.router = "llm"
        return plan

    async def _llm(self, question: str, history: list[dict[str, str]]) -> dict[str, Any]:
        convo = "\n".join(f"{h['role']}: {h['content'][:500]}" for h in history[-4:])
        result = await self.router_model.complete(
            [ChatMessage("system", ROUTER_PROMPT),
             ChatMessage("user", f"Conversation:\n{convo}\n\nLatest question: {question}")],
            max_tokens=300, temperature=0.0, json_mode=True, timeout=self.timeout_s,
        )
        content = result.content
        start, end = content.find("{"), content.rfind("}")
        return json.loads(content[start: end + 1]) if start >= 0 else {}
