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

import dataclasses
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from rag.answering.aggregate import AGGREGATE, Aggregator
from rag.answering.arithmetic import unworked_sums
from rag.answering.citations import citation_dicts, strip_unknown_markers, verify
from rag.answering.grounding import groundedness
from rag.answering.prompts import ANSWER_SYSTEM, CLOSEST, NOT_FOUND, RESEARCH_SYSTEM, answer_user_message
from rag.answering.strategy import Strategy, check_sufficiency, documents_named_in, plan_strategy, read_documents, search_parts
from rag.answering.tools import TOOLS, ToolBox
from rag.cache import digest, get_cache
from rag.errors import ConfigurationError, RateLimited
from rag.monitoring import inspector
from rag.monitoring.tracing import current_trace_id, span
from rag.plugins.capabilities import ChatMessage, Usage
from rag.retrieval.query_router import QueryRouter
from rag.retrieval.retriever import Retriever
from rag.retrieval.types import SearchFilters
from rag.security.guardrails import check_question, sanitize_evidence

log = logging.getLogger(__name__)

# a change to either prompt makes every cached answer out of date
_PROMPT_VERSION = digest(ANSWER_SYSTEM, RESEARCH_SYSTEM)

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
        self._strategy_passages: list[Any] | None = None
        self._candidate_docs: list[Any] = []
        self._read_max = 3  # documents a read_files strategy may read, and the characters they share (set per question)
        self._read_budget = 60_000

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
        inspector.note(
            "question", "Question received", question=question, checked_text=guard.text, blocked=guard.blocked,
            reason=guard.reason or None, model_choice=model_choice, filters=(filters or SearchFilters()).as_dict(),
            history=[{"role": h["role"], "content": inspector.clip(h["content"], 2000)} for h in history or []],
        )
        if guard.blocked:
            outcome.status = "refused"
            outcome.answer = guard.reason
            yield _event("error", {"message": guard.reason, "code": "guardrail"})
            return
        await self.services.hub.refresh_overrides()

        # An identical question from someone with the same access, against the same library, models and prompt, is answered
        # from the cache (only verified answers are stored, and any change to the library or to access bumps the epoch).
        cache = get_cache(self.settings)
        cache_key = await self._answer_key(cache, guard.text, principals, filters, model_choice, history)
        if cache_key:
            cached = await cache.get_json(cache_key)
            if cached:
                await cache.stat("answer_hit")
                inspector.note("cache", "Answer served from the cache: no search and no model calls", key=cache_key[:24],
                               why="the same question, by someone with the same access, against the same library, "
                                   "models and prompts was answered and verified before")
                async for event in self._replay(cached, outcome, started):
                    yield event
                return
            await cache.stat("answer_miss")

        yield _event("status", {"stage": "plan", "message": "Understanding the question"})
        t = time.perf_counter()
        async with span("plan", "retrieval"):
            with inspector.purpose("Rewriting the question (query router)"):
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
        # a model on this server has a small context: it keeps the old, smaller reading limits
        small = model_spec is not None and model_spec.is_local
        self._read_max = 3 if small else self.settings.read_files_max_docs
        self._read_budget = min(60_000, self.settings.read_files_budget_chars) if small else self.settings.read_files_budget_chars
        if model_spec is not None and not model_spec.is_local:
            plan.filters.exclude_local_only = True  # local-only documents never go to a cloud model
        outcome.filters = {**plan.filters.as_dict(), "query_type": plan.query_type, "identifiers": plan.identifiers,
                           "language": plan.language, "hard": plan.hard, "reasons": plan.reasons,
                           "router": plan.router}
        inspector.note(
            "plan", f"Query plan: type {plan.query_type}, language {plan.language}", ms=timings["plan"],
            query=plan.query, english=plan.english,
            language=plan.language, query_type=plan.query_type, identifiers=plan.identifiers, hard=plan.hard,
            needs_research=plan.needs_research, filters=plan.filters.as_dict(), boosts=plan.boosts, reasons=plan.reasons,
            planned_by=plan.router, answer_role=role, answer_model=model_label,
        )
        yield _event("filters", outcome.filters)

        yield _event("status", {"stage": "search", "message": "Searching the library"})
        t = time.perf_counter()
        async with span("retrieve", "retrieval") as s:
            result = await self.retriever.search(plan, principals)
            s.attrs.update({"passages": len(result.passages), "documents": len(result.documents)})
        timings["retrieve"] = (time.perf_counter() - t) * 1000
        timings.update({f"retrieve.{k}": v for k, v in result.timings_ms.items()})
        passages = list(result.passages)

        strategy = Strategy()
        if passages and await self._whole_sheet(plan, principals, guard.text, passages):
            strategy.name = "read_files"
            whole = passages[0].file_name or passages[0].title
            inspector.note("read", f"Read all of {whole}", why="a list-everything question about one spreadsheet that "
                           "fits the budget", documents=[{"file": whole, "chunks": len(passages),
                                                          "chars": sum(len(p.text) for p in passages), "cut": False}])
            yield _event("status", {"stage": "research", "message": f"Reading all of {whole}"})
        else:
            # how to answer is decided per question by a model (rules when there is none): see answering/strategy.py
            # files the question names are candidates even when the search ranked them low (or not at all)
            named = await documents_named_in(guard.text, principals, exclude_local_only=plan.filters.exclude_local_only)
            if named:
                inspector.note("named", f"{len(named)} file(s) named in the question",
                               files=[{"file": d.file_name, "coverage": d.score} for d in named])
            seen = {d.version_id for d in named}
            self._candidate_docs = (named + [d for d in result.documents if d.version_id not in seen])[: max(20, self._read_max)]
            if self.settings.strategy_planner and not plan.identifiers and (self._candidate_docs or passages):
                yield _event("status", {"stage": "plan", "message": "Deciding how to answer"})
                t = time.perf_counter()
                planner = self.services.hub.try_get("LLM_ANSWER")
                with inspector.purpose("Planning how to answer"):
                    strategy = (
                        await plan_strategy(planner, guard.text, self._candidate_docs, history, outcome.usage, self._read_max)
                        or Strategy()
                    )
                timings["strategy"] = (time.perf_counter() - t) * 1000
            if strategy.planned_by == "rules" and AGGREGATE.search(guard.text) and not plan.identifiers:
                strategy.name = "list_all"
            inspector.note(
                "strategy", f"Strategy: {strategy.name}", ms=timings.get("strategy"), strategy=strategy.name,
                chosen_by=strategy.planned_by, subject=strategy.subject, name_words=strategy.name_words,
                sub_questions=strategy.sub_questions,
                files=[self._candidate_docs[i - 1].file_name for i in strategy.files if i - 1 < len(self._candidate_docs)],
                candidates=[{"file": d.file_name or d.title, "named": getattr(d, "named", False)} for d in self._candidate_docs],
            )
            outcome.filters["strategy"] = strategy.name
            async for event in self._carry_out(strategy, plan, principals, guard.text, result, passages, outcome, timings):
                yield event
            passages = self._strategy_passages if self._strategy_passages is not None else passages
        if strategy.name == "lookup" and plan.needs_research and getattr(model, "supports_tools", False) and passages is not None:
            t = time.perf_counter()
            async for event in self._research(model, plan, principals, guard.text, passages, outcome.usage):
                yield event
            timings["research"] = (time.perf_counter() - t) * 1000

        # Before writing: does the evidence cover every part of the question? If not, look further for what is missing,
        # reading many documents in full (the corrective step of agentic RAG). One cheap model call when nothing is missing.
        if passages and self.settings.sufficiency_check and strategy.name != "list_all" and not (
        strategy.name == "read_files" and len(strategy.files) > 3
    ):
            yield _event("status", {"stage": "check", "message": "Checking the evidence covers every part of the question"})
            t = time.perf_counter()
            with inspector.purpose("Checking the evidence is complete"):
                verdict = await check_sufficiency(self.services.hub.try_get("LLM_ANSWER"), guard.text, passages, outcome.usage)
            inspector.note(
                "check",
                "Evidence check could not be made" if verdict is None
                else "Evidence covers the question" if verdict.complete else f"Evidence incomplete: {verdict.missing}",
                ms=(time.perf_counter() - t) * 1000, passages=len(passages),
                complete=None if verdict is None else verdict.complete, missing=None if verdict is None else verdict.missing,
            )
            if verdict is not None and not verdict.complete:
                missing = verdict.missing or guard.text
                yield _event("status", {"stage": "research", "message": f"Looking further for: {missing}"})
                further = await self.retriever.search(
                    dataclasses.replace(plan, query=missing, original=missing, english=None, identifiers=[]), principals
                )
                aggregator = Aggregator(self.services, self.retriever)
                async for event in aggregator.run(
                    f"{guard.text} (Still missing: {missing})", plan, principals, further, outcome.usage
                ):
                    yield event
                if aggregator.passages:
                    known = {(p.version_id, p.page_start) for p in passages}
                    added = [p for p in aggregator.passages if (p.version_id, p.page_start) not in known]
                    passages = [dataclasses.replace(p, n=i) for i, p in enumerate((passages + added)[:48], start=1)]
                outcome.filters["corrected"] = True
            timings["check"] = (time.perf_counter() - t) * 1000

        if not passages:
            language = "ar" if plan.language == "ar" else "en"
            text = NOT_FOUND[language]
            outcome.answer, outcome.status = text, "not_found"
            inspector.note("evidence", "No evidence found: answered 'not found' without calling the answer model",
                           off_topic=result.off_topic, signals=result.signals)
            yield _event("delta", {"text": text})
            done = self._done(outcome, result, timings, started)
            self._note_done(done)
            yield _event("done", done)
            return

        for p in passages:
            p.text = sanitize_evidence(p.text)
        inspector.note(
            "evidence", f"{len(passages)} passages go to the answer model ({sum(len(p.text) for p in passages):,} characters)",
            passages=[{"n": p.n, "file": p.file_name or p.title, "pages": [p.page_start, p.page_end], "chars": len(p.text),
                       "from": p.chunk_id.split(":", 1)[0] if ":" in p.chunk_id else "search", "score": p.score,
                       "text": inspector.clip(p.text, 6000)} for p in passages],
        )
        yield _event("status", {"stage": "write", "message": f"Writing the answer with {model_label}", "model": model_label})
        messages = [ChatMessage("system", ANSWER_SYSTEM), ChatMessage("user", answer_user_message(guard.text, passages, history))]
        t = time.perf_counter()
        parts: list[str] = []
        try:
            with inspector.purpose("Writing the answer"):
                async for chunk in self._stream_with_fallback(model, messages, outcome):
                    if chunk.get("delta"):
                        parts.append(chunk["delta"])
                        yield _event("delta", {"text": chunk["delta"]})
                    elif chunk.get("status"):
                        yield _event("status", chunk["status"])
        except RateLimited as exc:
            outcome.status = "error"
            yield _event(
                "error",
                {
                    "message": "The answer model is rate limited; try again in a moment.",
                    "code": "rate_limited",
                    "retry_after": exc.retry_after,
                },
            )
            return
        except Exception as exc:
            log.exception("answer generation failed")
            outcome.status = "error"
            yield _event("error", {"message": f"The answer model failed ({type(exc).__name__}).", "code": "model"})
            return
        timings["generate"] = (time.perf_counter() - t) * 1000

        answer = strip_unknown_markers("".join(parts), passages)
        sums = unworked_sums(answer)  # totals the model wrote out but did not work out: calculated here, exactly
        if sums:
            note = "\n\n**Calculated:** " + "; ".join(sums)
            answer += note
            inspector.note("calc", f"Calculated {len(sums)} sum(s) the answer left unworked", sums=sums,
                           how="exact decimal arithmetic on the numbers the answer added up")
            yield _event("delta", {"text": note})
        t = time.perf_counter()
        citations = verify(answer, passages)
        with inspector.purpose("Checking the answer is supported"):
            report = await groundedness(self.services.hub.groundedness(), answer, passages)
        timings["verify"] = (time.perf_counter() - t) * 1000
        inspector.note(
            "citations", f"{sum(c.verified for c in citations)} of {len(citations)} citations verified",
            ms=timings["verify"],
            citations=[{"n": c.n, "file": c.passage.file_name or c.passage.title, "page": c.passage.page_start,
                        "verified": c.verified, "quote": c.quote, "missing_numbers": c.missing_numbers} for c in citations],
            groundedness=round(report.score, 3), judge=report.judge, uncited_claims=report.uncited_claims,
            sentences=report.sentences[:60],
        )
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
        if cache_key and status == "ok":
            await cache.set_json(
                cache_key,
                {
                    "answer": answer,
                    "citations": outcome.citations,
                    "groundedness": report.score,
                    "model": outcome.model,
                    "filters": outcome.filters,
                    "done": {
                        k: v
                        for k, v in done.items()
                        if k not in {"trace_id", "timings_ms", "usage", "message_id", "conversation_id"}
                    },
                },
                self.settings.cache_answer_ttl_s,
            )
        self._note_done(done)
        yield _event("done", done)

    @staticmethod
    def _note_done(done: dict[str, Any]) -> None:
        usage = done.get("usage") or {}
        inspector.note("done", f"Answer finished: {done.get('status')}", answer_status=done.get("status"),
                       model=done.get("model"),
                       usage=usage, timings_ms=done.get("timings_ms"), groundedness=done.get("groundedness"))

    async def _answer_key(
        self,
        cache: Any,
        question: str,
        principals: list[str],
        filters: SearchFilters | None,
        model_choice: str | None,
        history: list[dict[str, str]] | None,
    ) -> str | None:
        """The cache key of an answer, or None when this question must not be cached (no Redis, caching off, or a follow-up
        whose meaning depends on the conversation)."""
        s = self.settings
        if not (s.cache_answers and cache.enabled) or history:
            return None
        hub = self.services.hub
        models = [hub.spec(role).label for role in ("LLM_ANSWER", "LLM_ANSWER_HARD", "LLM_ROUTER", "LLM_ANSWER_FALLBACK")]
        epoch = await cache.epoch()
        return "ans:" + digest(
            question.strip(),
            sorted(principals),
            (filters or SearchFilters()).as_dict(),
            model_choice,
            models,
            epoch,
            _PROMPT_VERSION,
            s.llm_answer_max_tokens,
        )

    async def _replay(self, cached: dict[str, Any], outcome: AnswerOutcome, started: float) -> AsyncIterator[dict[str, Any]]:
        """Send a stored answer through the same events a fresh one would produce, at no cost."""
        outcome.answer, outcome.status = cached["answer"], "ok"
        outcome.citations, outcome.groundedness = cached["citations"], cached.get("groundedness")
        outcome.model, outcome.filters = cached.get("model", ""), cached.get("filters", {})
        yield _event("filters", outcome.filters)
        yield _event("status", {"stage": "write", "message": "Answer found in the cache", "model": outcome.model})
        text = outcome.answer
        step = max(40, len(text) // 6)
        for i in range(0, len(text), step):
            yield _event("delta", {"text": text[i : i + step]})
        for citation in outcome.citations:
            yield _event("citation", citation)
        done = dict(cached.get("done") or {})
        done.update(
            {
                "answer": outcome.answer,
                "status": "ok",
                "model": outcome.model,
                "cached": True,
                "trace_id": current_trace_id(),
                "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                "timings_ms": {"total": round((time.perf_counter() - started) * 1000, 1), "cache": 1.0},
            }
        )
        self._note_done(done)
        yield _event("done", done)

    # ---- generation ----------------------------------------------------------------------------------------------------
    async def _stream_with_fallback(
        self, model: Any, messages: list[ChatMessage], outcome: AnswerOutcome
    ) -> AsyncIterator[dict[str, Any]]:
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

    async def _carry_out(
        self,
        strategy: Strategy,
        plan: Any,
        principals: list[str],
        question: str,
        result: Any,
        passages: list[Any],
        outcome: AnswerOutcome,
        timings: dict[str, float],
    ) -> AsyncIterator[dict[str, Any]]:
        """Gather the evidence the chosen strategy needs. The passages end up in self._strategy_passages (None: keep the
        search's)."""
        self._strategy_passages = None
        t = time.perf_counter()
        if strategy.name == "read_files":
            docs = [self._candidate_docs[i - 1] for i in strategy.files if i - 1 < len(self._candidate_docs)]
            names = ", ".join(d.file_name or d.title for d in docs) if len(docs) <= 3 else f"{len(docs)} documents"
            yield _event("status", {"stage": "research", "message": f"Reading in full: {names}"})
            read, cut = await read_documents(docs, self._read_budget)
            if read:
                # the search's best passages from other documents stay as well: the answer may sit in a file nobody named
                # (a lease's expiry in its registration form); for a document too long to read whole, its found passages too
                read_versions = {d.version_id for d in docs}
                known = {r.chunk_id for r in read}
                extra = [p for p in passages if (p.file_name or p.title) in cut and p.chunk_id not in known]
                others = [p for p in passages if p.version_id not in read_versions][:6]
                self._strategy_passages = [dataclasses.replace(p, n=i) for i, p in enumerate(read + extra + others, start=1)]
        elif strategy.name == "list_all":
            aggregator = Aggregator(self.services, self.retriever)
            words = strategy.name_words + ([strategy.subject] if strategy.subject else [])
            async for event in aggregator.run(question, plan, principals, result, outcome.usage, words=words):
                yield event
            if aggregator.passages:
                self._strategy_passages = aggregator.passages
        elif strategy.name == "lookup" and named_doc(self._candidate_docs):
            # the question names a file: read it (up to a budget) next to the search's passages, so details on its other
            # pages or sheets are not missed
            doc = self._candidate_docs[0]
            yield _event("status", {"stage": "research", "message": f"Reading {doc.file_name or doc.title}"})
            read, cut = await read_documents([doc], self.settings.named_file_chars)
            if cut:
                # too long to read whole: its beginning (titles, the first sheet) plus the parts that match the question
                read, _ = await read_documents([doc], self.settings.named_file_chars // 3)
                inside = dataclasses.replace(plan, filters=dataclasses.replace(plan.filters, document_ids=[doc.document_id]))
                heads = {r.text[:300] for r in read}
                read += [
                    p
                    for p in (await self.retriever.search(inside, principals, passages=12)).passages
                    if p.text[:300] not in heads
                ]
            known = {r.text[:300] for r in read}
            merged = read + [p for p in passages if p.text[:300] not in known][:12]  # the search's own hits, in that file too
            if read:
                self._strategy_passages = [dataclasses.replace(p, n=i) for i, p in enumerate(merged, start=1)]
        elif strategy.name == "multi_part":
            yield _event(
                "status", {"stage": "research", "message": f"Searching {len(strategy.sub_questions)} parts of the question"}
            )
            found = await search_parts(self.retriever, plan, principals, strategy.sub_questions, per_part=6, limit=30)
            if found:
                self._strategy_passages = found
        timings[f"strategy.{strategy.name}"] = (time.perf_counter() - t) * 1000

    async def _whole_sheet(self, plan: Any, principals: list[str], question: str, passages: list[Any]) -> bool:
        """ "List all ..." about one spreadsheet: read the whole sheet at once instead of searching it row by row.

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

    async def _research(
        self, model: Any, plan: Any, principals: list[str], question: str, passages: list[Any], usage: Usage
    ) -> AsyncIterator[dict[str, Any]]:
        toolbox = ToolBox(self.retriever, plan, principals, passages, max_pages=self.settings.agent_max_pages)
        summary = "\n".join(f"[{p.n}] {p.label()}" for p in passages) or "(nothing yet)"
        messages = [
            ChatMessage("system", RESEARCH_SYSTEM),
            ChatMessage("user", f"Question: {question}\n\nPassages found so far:\n{summary}"),
        ]
        stale = 0
        for step in range(self.settings.agent_max_steps):
            try:
                with inspector.purpose(f"Research step {step + 1}"):
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
                yield _event("status", {"stage": "research", "message": _describe(call.name, call.arguments), "step": step + 1})
                t = time.perf_counter()
                async with span(f"tool.{call.name}", "tool", tool=call.name):
                    output = await toolbox.run(call.name, call.arguments)
                inspector.note("tool", _describe(call.name, call.arguments), ms=(time.perf_counter() - t) * 1000,
                               tool=call.name, arguments=call.arguments, output=inspector.clip(output, 12000))
                messages.append(ChatMessage("tool", output[:12000], tool_call_id=call.id, name=call.name))
            stale = 0 if len(passages) > before else stale + 1
            if stale >= 2:  # two steps in a row found nothing new: more searching will not help
                return

    def _done(self, outcome: AnswerOutcome, result: Any, timings: dict[str, float], started: float) -> dict[str, Any]:
        timings["total"] = (time.perf_counter() - started) * 1000
        return {
            "answer": outcome.answer,
            "status": outcome.status,
            "model": outcome.model,
            "groundedness": outcome.groundedness,
            "citations": len(outcome.citations),
            "timings_ms": {k: round(v, 1) for k, v in timings.items()},
            "trace_id": current_trace_id(),
            "usage": {
                "input_tokens": outcome.usage.input_tokens,
                "output_tokens": outcome.usage.output_tokens,
                "cost_usd": outcome.usage.cost_usd,
            },
            "documents": [
                {
                    "document_id": d.document_id,
                    "doc_number": d.doc_number,
                    "title": d.title,
                    "pages": d.pages,
                    "file_name": d.file_name,
                }
                for d in result.documents[:5]
            ],
        }


def named_doc(candidates: list[Any]) -> bool:
    """The best candidate is a file the question names (most of its file name is in the question)."""
    return bool(candidates) and getattr(candidates[0], "named", False) and candidates[0].score >= 0.75


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
