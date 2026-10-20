"""Answering a question, as a stream of events for the chat UI (sent as server-sent events):

  status     what is happening now ("Searching 3 ways", "Reading S-201 pages 3-4", "Writing with claude-haiku-5.5")
  filters    the plan: question type, identifiers, filters applied, reasons
  delta      answer text as it is written
  citation   one verified (or flagged) citation per passage cited: document, page, quote
  done       final answer, status (ok | not_found | unverified), groundedness, model, timings, closest documents
  error      a problem the user should see (rate limit, configuration)

Flow: guardrails -> query plan -> hybrid retrieval -> research loop with tools (hard questions, tool-capable models)
-> streamed answer (fallback model on failure) -> citation verification -> groundedness -> "not found" when the
evidence does not support an answer.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from rag.answering.citations import citation_dicts, strip_unknown_markers, verify
from rag.answering.grounding import groundedness
from rag.answering.prompts import ANSWER_SYSTEM, CLOSEST, NOT_FOUND, RESEARCH_SYSTEM, answer_user_message
from rag.answering.tools import TOOLS, ToolBox
from rag.errors import ConfigurationError, RateLimited
from rag.monitoring.tracing import current_trace_id, span
from rag.plugins.capabilities import ChatMessage, Usage
from rag.retrieval.query_router import QueryRouter
from rag.retrieval.retriever import Retriever
from rag.retrieval.types import SearchFilters
from rag.security.guardrails import check_question, sanitize_evidence

log = logging.getLogger(__name__)

_NOT_FOUND_HINTS = ("could not find", "couldn't find", "do not contain", "don't contain", "does not contain",
                    "not mentioned", "no information", "لم أجد", "لا تحتوي")
_ENUMERATE = re.compile(r"(?i)\b(all|every|each|list|complete|entire|whole|full|everything)\b|جميع|كل\s")
UNVERIFIED_NOTE = {
    "en": "\n\nNote: I could not match every statement above to the cited pages; check the sources before relying "
          "on it.",
    "ar": "\n\nملاحظة: لم أتمكن من مطابقة كل ما ورد أعلاه مع الصفحات المذكورة؛ يرجى مراجعة المصادر.",
}


@dataclass
class AnswerOutcome:
    answer: str = ""
    status: str = "ok"
    citations: list[dict[str, Any]] = field(default_factory=list)
    groundedness: float | None = None
    model: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)


def _event(name: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"event": name, "data": data}


class Answerer:
    def __init__(self, services: Any) -> None:
        self.services = services
        self.settings = services.settings
        self.retriever = Retriever(services)

    async def router(self) -> QueryRouter:
        hub = self.services.hub
        return QueryRouter(known_projects=await self.services.known_projects(), router_model=hub.try_get("LLM_ROUTER"),
                           timeout_s=self.settings.llm_router_timeout_s,
                           reranker_languages=self.settings.reranker_language_set)

    async def answer(self, question: str, *, principals: list[str], filters: SearchFilters | None = None,
                     history: list[dict[str, str]] | None = None, model_choice: str | None = None,
                     outcome: AnswerOutcome | None = None) -> AsyncIterator[dict[str, Any]]:
        outcome = outcome or AnswerOutcome()
        started = time.perf_counter()
        timings: dict[str, float] = {}
        guard = check_question(question)
        if guard.blocked:
            outcome.status = "refused"
            outcome.answer = guard.reason
            yield _event("error", {"message": guard.reason, "code": "guardrail"})
            return
        await self.services.hub.refresh_overrides()

        yield _event("status", {"stage": "plan", "message": "Understanding the question"})
        t = time.perf_counter()
        async with span("plan", "retrieval"):
            plan = await (await self.router()).plan(guard.text, filters or SearchFilters(), history)
        timings["plan"] = (time.perf_counter() - t) * 1000

        role = "LLM_ANSWER_HARD" if plan.hard and not model_choice else "LLM_ANSWER"
        try:
            model = self.services.hub.chat(role, choice=model_choice)
        except ConfigurationError as exc:
            outcome.status = "error"
            yield _event("error", {"message": str(exc), "code": "configuration"})
            return
        model_spec = getattr(model, "spec", None)
        model_label = model_spec.label if model_spec is not None else role
        outcome.model = model_label
        if model_spec is not None and not model_spec.is_local:
            plan.filters.exclude_local_only = True  # local-only documents never go to a cloud model
        outcome.filters = {**plan.filters.as_dict(), "query_type": plan.query_type, "identifiers": plan.identifiers,
                           "language": plan.language, "hard": plan.hard, "reasons": plan.reasons,
                           "router": plan.router}
        yield _event("filters", outcome.filters)

        yield _event("status", {"stage": "search", "message": "Searching the library"})
        t = time.perf_counter()
        async with span("retrieve", "retrieval") as s:
            result = await self.retriever.search(plan, principals)
            s.attrs.update({"passages": len(result.passages), "documents": len(result.documents)})
        timings["retrieve"] = (time.perf_counter() - t) * 1000
        timings.update({f"retrieve.{k}": v for k, v in result.timings_ms.items()})
        passages = list(result.passages)

        if passages and await self._whole_sheet(plan, principals, guard.text, passages):
            yield _event("status", {"stage": "research", "message": f"Reading all of {passages[0].file_name or passages[0].title}"})
        elif plan.needs_research and getattr(model, "supports_tools", False) and passages is not None:
            t = time.perf_counter()
            async for event in self._research(model, plan, principals, guard.text, passages, outcome.usage):
                yield event
            timings["research"] = (time.perf_counter() - t) * 1000

        if not passages:
            language = "ar" if plan.language == "ar" else "en"
            text = NOT_FOUND[language]
            outcome.answer, outcome.status = text, "not_found"
            yield _event("delta", {"text": text})
            yield _event("done", self._done(outcome, result, timings, started))
            return

        for p in passages:
            p.text = sanitize_evidence(p.text)
        yield _event("status", {"stage": "write", "message": f"Writing the answer with {model_label}",
                                "model": model_label})
        messages = [ChatMessage("system", ANSWER_SYSTEM),
                    ChatMessage("user", answer_user_message(guard.text, passages, history))]
        t = time.perf_counter()
        parts: list[str] = []
        try:
            async for chunk in self._stream_with_fallback(model, messages, outcome):
                if chunk.get("delta"):
                    parts.append(chunk["delta"])
                    yield _event("delta", {"text": chunk["delta"]})
                elif chunk.get("status"):
                    yield _event("status", chunk["status"])
        except RateLimited as exc:
            outcome.status = "error"
            yield _event("error", {"message": "The answer model is rate limited; try again in a moment.",
                                   "code": "rate_limited", "retry_after": exc.retry_after})
            return
        except Exception as exc:
            log.exception("answer generation failed")
            outcome.status = "error"
            yield _event("error", {"message": f"The answer model failed ({type(exc).__name__}).", "code": "model"})
            return
        timings["generate"] = (time.perf_counter() - t) * 1000

        answer = strip_unknown_markers("".join(parts), passages)
        t = time.perf_counter()
        citations = verify(answer, passages)
        report = await groundedness(self.services.hub.groundedness(), answer, passages)
        timings["verify"] = (time.perf_counter() - t) * 1000
        for c in citations:
            yield _event("citation", c.as_event())

        language = "ar" if plan.language == "ar" else "en"
        lowered = answer.lower()
        if not citations and any(h in lowered for h in _NOT_FOUND_HINTS):
            status = "not_found"
        elif citations and not any(c.verified for c in citations) and report.score < 0.5:
            status = "unverified"
            note = UNVERIFIED_NOTE[language]
            answer += note
            yield _event("delta", {"text": note})
        elif not citations:
            status = "unverified"
        else:
            status = "ok"
        outcome.answer, outcome.status = answer, status
        outcome.citations = citation_dicts(citations)
        outcome.groundedness = report.score
        done = self._done(outcome, result, timings, started)
        done["grounding"] = {"score": report.score, "judge": report.judge, "uncited_claims": report.uncited_claims}
        if status == "not_found":
            done["closest_label"] = CLOSEST[language]
        yield _event("done", done)

    # ---- generation ----------------------------------------------------------------------------------------------------
    async def _stream_with_fallback(self, model: Any, messages: list[ChatMessage],
                                    outcome: AnswerOutcome) -> AsyncIterator[dict[str, Any]]:
        """Stream from the answer model; if it fails before writing anything, switch to LLM_ANSWER_FALLBACK."""
        candidates = [model]
        fallback = self.services.hub.try_get("LLM_ANSWER_FALLBACK")
        if fallback is not None and getattr(fallback, "spec", None) != getattr(model, "spec", None):
            candidates.append(fallback)
        last_error: Exception | None = None
        for index, candidate in enumerate(candidates):
            wrote = False
            try:
                async for chunk in candidate.stream(messages, max_tokens=self.settings.llm_answer_max_tokens):
                    if chunk.delta:
                        wrote = True
                        yield {"delta": chunk.delta}
                    if chunk.result is not None:
                        outcome.usage.add(chunk.result.usage)
                if index > 0:
                    outcome.model = candidate.spec.label
                return
            except Exception as exc:
                if wrote or index == len(candidates) - 1:
                    raise
                last_error = exc
                label = getattr(getattr(candidates[index + 1], "spec", None), "label", "fallback model")
                log.warning("answer model failed (%s); falling back to %s", type(exc).__name__, label)
                yield {"status": {"stage": "write", "message": f"Switching to {label}", "model": label}}
        if last_error:
            raise last_error

    async def _whole_sheet(self, plan: Any, principals: list[str], question: str, passages: list[Any]) -> bool:
        """"List all ..." about one spreadsheet: read the whole sheet at once instead of searching it row by row.

        Only when most of the best passages come from one workbook/CSV and all of it fits the budget; otherwise the
        research loop runs as before."""
        if not _ENUMERATE.search(question):
            return False
        top = passages[0]
        name = (top.file_name or "").lower()
        if not name.endswith((".xlsx", ".xlsm", ".xls", ".ods", ".csv", ".tsv")):
            return False
        same = sum(1 for p in passages[:6] if p.version_id == top.version_id)
        if same < min(3, len(passages[:6])):
            return False
        toolbox = ToolBox(self.retriever, plan, principals, passages)
        return await toolbox.read_whole(top, self.settings.agent_whole_doc_chars)

    async def _research(self, model: Any, plan: Any, principals: list[str], question: str, passages: list[Any],
                        usage: Usage) -> AsyncIterator[dict[str, Any]]:
        toolbox = ToolBox(self.retriever, plan, principals, passages, max_pages=self.settings.agent_max_pages)
        summary = "\n".join(f"[{p.n}] {p.label()}" for p in passages) or "(nothing yet)"
        messages = [ChatMessage("system", RESEARCH_SYSTEM),
                    ChatMessage("user", f"Question: {question}\n\nPassages found so far:\n{summary}")]
        stale = 0
        for step in range(self.settings.agent_max_steps):
            try:
                result = await model.complete(messages, tools=TOOLS, max_tokens=600, temperature=0.0)
            except Exception:
                log.warning("research step failed; answering with the passages found", exc_info=True)
                return
            usage.add(result.usage)
            if not result.tool_calls:
                return
            messages.append(ChatMessage("assistant", result.content or "", tool_calls=result.tool_calls))
            before = len(passages)
            for call in result.tool_calls[:4]:
                yield _event("status", {"stage": "research", "message": _describe(call.name, call.arguments),
                                        "step": step + 1})
                async with span(f"tool.{call.name}", "tool", tool=call.name):
                    output = await toolbox.run(call.name, call.arguments)
                messages.append(ChatMessage("tool", output[:12000], tool_call_id=call.id, name=call.name))
            stale = 0 if len(passages) > before else stale + 1
            if stale >= 2:  # two steps in a row found nothing new: more searching will not help
                return

    def _done(self, outcome: AnswerOutcome, result: Any, timings: dict[str, float], started: float) -> dict[str, Any]:
        timings["total"] = (time.perf_counter() - started) * 1000
        return {
            "answer": outcome.answer, "status": outcome.status, "model": outcome.model,
            "groundedness": outcome.groundedness, "citations": len(outcome.citations),
            "timings_ms": {k: round(v, 1) for k, v in timings.items()}, "trace_id": current_trace_id(),
            "usage": {"input_tokens": outcome.usage.input_tokens, "output_tokens": outcome.usage.output_tokens,
                      "cost_usd": outcome.usage.cost_usd},
            "documents": [{"document_id": d.document_id, "doc_number": d.doc_number, "title": d.title,
                           "pages": d.pages,
                           "file_name": d.file_name} for d in result.documents[:5]],
        }


def _describe(tool: str, args: dict[str, Any]) -> str:
    if tool == "search":
        return f"Searching for “{str(args.get('query', ''))[:80]}”"
    if tool == "search_within":
        return f"Searching inside {args.get('document')}"
    if tool == "read_pages":
        pages = args.get("pages") or []
        return f"Reading {args.get('document')} page{'s' if len(pages) != 1 else ''} {', '.join(map(str, pages))}"
    if tool == "open_reference":
        return f"Opening {args.get('identifier')}"
    return tool
