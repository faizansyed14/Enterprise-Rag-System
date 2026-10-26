"""Hybrid retrieval: filters -> three legs in one Qdrant request -> weighted fusion -> family demotion ->
language-gated rerank -> document candidates re-checked in Postgres -> evidence passages.

  dense leg     embedding similarity (multilingual)                       top DENSE_TOP_K
  sparse leg    BM25 with identifier tokens                              top SPARSE_TOP_K
  exact leg     chunks containing an identifier from the question,
                ranked by embedding similarity                           top EXACT_TOP_K
When the filtered set is smaller than EXACT_SEARCH_BELOW chunks (one project, a few documents) the
dense leg is an exact scan instead of an approximate graph search, so narrow searches lose nothing.

Access control is enforced in the Qdrant filter (the user's principals must overlap the chunk's ACL) and again in
Postgres for every document that could be shown.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict, defaultdict
from typing import Any

from qdrant_client import models
from sqlalchemy import text

from rag.cache import digest, get_cache
from rag.db.engine import transaction
from rag.monitoring import inspector
from rag.providers.vectorstore.qdrant import DENSE, SPARSE, match_any, match_value
from rag.retrieval import relevance
from rag.retrieval.types import Candidate, DocumentHit, Passage, QueryPlan, RetrievalResult, SearchFilters
from rag.text.bm25 import STOP_EN
from rag.text.language import languages_compatible

log = logging.getLogger(__name__)

_WORDS = re.compile(r"\w+", re.UNICODE)
BOOST = 0.08


def build_filter(principals: list[str], filters: SearchFilters, *, extra: list[Any] | None = None) -> models.Filter:
    must: list[Any] = [match_any("acl", principals), match_value("published", True)]
    if filters.project_ids:
        must.append(match_any("project_ids", filters.project_ids))
    if filters.doc_types:
        must.append(match_any("doc_type", filters.doc_types))
    if filters.document_ids:
        must.append(match_any("document_id", filters.document_ids))
    if filters.source_ids:
        must.append(match_any("source_ids", filters.source_ids))
    must_not: list[Any] = []
    if filters.exclude_local_only:
        must_not.append(match_value("local_only", True))
    return models.Filter(must=must + list(extra or []), must_not=must_not or None)


def _normalise(scores: dict[str, float]) -> dict[str, float]:
    """Scale a leg's scores to [0, 1] by its best score (lower bound 0). Unlike min-max, this keeps ratios: two
    near-identical scores stay near-identical instead of becoming 0 and 1 when a leg returns only a few hits."""
    if not scores:
        return {}
    hi = max(scores.values())
    if hi <= 1e-9:
        return dict.fromkeys(scores, 0.0)
    return {k: max(0.0, v) / hi for k, v in scores.items()}


def _minmax(scores: dict[str, float]) -> dict[str, float]:
    """Scale scores to [0, 1] between the worst and the best of the group (a group with no spread scores all 0)."""
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    span = hi - lo
    return {k: (v - lo) / span if span > 1e-12 else 0.0 for k, v in scores.items()}


class Retriever:
    def __init__(self, services: Any) -> None:
        self.services = services
        self.settings = services.settings

    async def search(self, plan: QueryPlan, principals: list[str], *, rerank: bool = True,
                     passages: int | None = None, debug: bool = False) -> RetrievalResult:
        s = self.settings
        timings: dict[str, float] = {}
        t0 = time.perf_counter()
        build = await self.services.live_build()
        embedder = self.services.embedder_for(build)
        flt = build_filter(principals, plan.filters)

        t = time.perf_counter()
        dense_vector = await embedder.embed_query(plan.query)
        timings["embed"] = (time.perf_counter() - t) * 1000

        visible = await self.services.vectors.count(build.collection, flt, exact=False)
        exact_scan = visible <= s.exact_search_below
        params = models.SearchParams(
            exact=exact_scan, hnsw_ef=max(128, s.dense_top_k * 2),
            quantization=models.QuantizationSearchParams(rescore=True, oversampling=s.qdrant_oversampling))

        requests: list[models.QueryRequest] = []
        legs: list[str] = []
        requests.append(models.QueryRequest(query=dense_vector, using=DENSE, filter=flt, limit=s.dense_top_k,
                                            with_payload=True, params=params))
        legs.append("dense")
        sparse = self.services.analyzer.query_vector(plan.query)
        if not sparse.is_empty():
            requests.append(models.QueryRequest(query=models.SparseVector(indices=sparse.indices, values=sparse.values),
                                                using=SPARSE, filter=flt, limit=s.sparse_top_k, with_payload=True))
            legs.append("sparse")
        if plan.english:
            english = self.services.analyzer.query_vector(plan.english)
            if not english.is_empty():
                requests.append(models.QueryRequest(
                    query=models.SparseVector(indices=english.indices, values=english.values), using=SPARSE,
                    filter=flt, limit=s.sparse_top_k, with_payload=True))
                legs.append("sparse_en")
        if plan.identifiers:
            id_filter = build_filter(principals, plan.filters, extra=[match_any("identifiers", plan.identifiers)])
            requests.append(models.QueryRequest(query=dense_vector, using=DENSE, filter=id_filter,
                                                limit=s.exact_top_k, with_payload=True,
                                                params=models.SearchParams(exact=True)))
            legs.append("exact")

        t = time.perf_counter()
        results = await self.services.vectors.search_batch(build.collection, requests)
        timings["search"] = (time.perf_counter() - t) * 1000

        candidates: dict[str, Candidate] = {}
        for leg, hits in zip(legs, results, strict=True):
            for hit in hits:
                cand = candidates.setdefault(hit.id, Candidate(id=hit.id, payload=hit.payload))
                cand.legs[leg] = max(cand.legs.get(leg, 0.0), hit.score)

        t = time.perf_counter()
        self._fuse(plan, candidates, legs)
        ranked = sorted(candidates.values(), key=lambda c: c.fused, reverse=True)
        ranked = self._demote_families(ranked)
        ranked = self._name_boost(plan, ranked)
        timings["fuse"] = (time.perf_counter() - t) * 1000

        await self._hydrate(ranked[: max(s.rerank_top_n, 40)])
        ranked = self._phrase_boost(plan, ranked)

        t = time.perf_counter()
        rerank_info: dict[str, Any] = {}
        if rerank:
            ranked = await self._rerank(plan, ranked, rerank_info)
        else:
            for c in ranked:
                c.final = c.fused
        named = [c for c in ranked if c.payload.get("_named")]
        if named:  # the reranker rescales every score, so a spelled-out file name is added back on top of its result
            for c in named:
                c.final += s.name_boost
            ranked = sorted(ranked, key=lambda c: c.final, reverse=True)
        timings["rerank"] = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        ranked = self._page_aggregate(ranked)
        allowed = await self._recheck(ranked[: max(s.candidate_docs * 4, 60)], principals, plan.filters)
        ranked = [c for c in ranked if c.version_id in allowed]
        documents = self._documents(ranked)
        restricted = 0  # local-only documents hidden from a cloud model: not counted yet (see docs/recommendations.md)
        sig = relevance.compute(self.services.analyzer, plan.query, plan.identifiers, ranked)
        # The cross-encoder's probability was calibrated on English questions; other scripts may score lower for the same
        # relevance, so until measured they are never turned away on it.
        min_rerank = s.min_rerank if plan.language in {"en", "latin"} else 0.0
        off_topic = relevance.is_off_topic(sig, min_dense=s.min_dense, min_sparse=s.min_sparse,
                                           min_coverage=s.min_coverage, min_rerank=min_rerank)
        if off_topic:
            chosen, passages_out = [], []
        else:
            chosen = self._choose_passages(ranked, passages or s.evidence_passages)
            passages_out = await self._expand(chosen)
        timings["candidates"] = (time.perf_counter() - t) * 1000
        timings["total"] = (time.perf_counter() - t0) * 1000

        result = RetrievalResult(plan=plan, passages=passages_out, documents=documents, timings_ms=timings,
                                 restricted_local_only=restricted, signals=sig.as_dict(), off_topic=off_topic)
        if inspector.current() is not None:
            inspector.note("search", f"Searched: {plan.query[:90]}", ms=timings["total"], **self._details(
                plan, legs, results, ranked, result, visible=visible, exact_scan=exact_scan, rerank=rerank_info))
        if debug:
            result.debug = {
                "collection": build.collection, "visible_chunks": visible, "exact_scan": exact_scan, "legs": {
                    leg: [{"id": h.id, "score": round(h.score, 4), "doc": h.payload.get("doc_number") or
                           h.payload.get("file_name"), "page": h.payload.get("page_start")} for h in hits[:20]]
                    for leg, hits in zip(legs, results, strict=True)},
                "ranked": [{"id": c.id, "doc": c.payload.get("doc_number") or c.payload.get("file_name"),
                            "file_name": c.payload.get("file_name", ""), "document_id": c.document_id,
                            "page": c.payload.get("page_start"), "page_end": c.payload.get("page_end"),
                            "legs": {k: round(v, 4) for k, v in c.legs.items()},
                            "fused": round(c.fused, 4), "rerank": None if c.rerank is None else round(c.rerank, 4),
                            "final": round(c.final, 4), "demoted": c.demoted,
                            "text": c.payload.get("text", "")[:200]} for c in ranked[:40]],
                "filter": flt.model_dump(exclude_none=True),
            }
        return result

    def _details(self, plan: QueryPlan, legs: list[str], results: list[Any], ranked: list[Candidate],
                 result: RetrievalResult, **extra: Any) -> dict[str, Any]:
        """One search as the chat's Details panel shows it: each leg's hits, the candidates with every score, the passages."""
        s = self.settings
        return {
            "query": plan.query,
            "english": plan.english,
            "identifiers": plan.identifiers,
            "filters": plan.filters.as_dict(),
            "legs": {leg: len(hits) for leg, hits in zip(legs, results, strict=True)},
            "limits": {"dense": s.dense_top_k, "sparse": s.sparse_top_k, "exact": s.exact_top_k,
                       "rerank_top_n": s.rerank_top_n, "passages": len(result.passages)},
            "candidates": [
                {
                    "rank": i,
                    "file": c.payload.get("file_name") or c.payload.get("title", ""),
                    "page": c.payload.get("page_start"),
                    "chunk": c.payload.get("chunk_no"),
                    "legs": {k: round(v, 4) for k, v in c.legs.items()},
                    "fused": round(c.fused, 4),
                    "phrase": round(c.phrase, 3),
                    "rerank": None if c.rerank is None else round(c.rerank, 4),
                    "final": round(c.final, 4),
                    "demoted": c.demoted,
                    "named": bool(c.payload.get("_named")),
                    "text": inspector.clip(c.payload.get("text", ""), 1500),
                }
                for i, c in enumerate(ranked[:30], start=1)
            ],
            "passages": [
                {"n": p.n, "file": p.file_name or p.title, "pages": [p.page_start, p.page_end], "score": p.score,
                 "chars": len(p.text), "text": inspector.clip(p.text, 6000)}
                for p in result.passages
            ],
            "documents": [{"file": d.file_name or d.title, "score": d.score, "pages": d.pages} for d in result.documents],
            "signals": result.signals,
            "off_topic": result.off_topic,
            "timings_ms": {k: round(v, 1) for k, v in result.timings_ms.items()},
            **extra,
        }

    # ---- fusion -----------------------------------------------------------------------------------------------------
    def _fuse(self, plan: QueryPlan, candidates: dict[str, Candidate], legs: list[str]) -> None:
        weights = dict(self.settings.fusion_weight_table.get(plan.query_type) or
                       self.settings.fusion_weight_table["question"])
        weights.setdefault("sparse_en", weights.get("sparse", 0.3) * 0.8)
        normalised = {leg: _normalise({c.id: c.legs[leg] for c in candidates.values() if leg in c.legs})
                      for leg in legs}
        if "exact" in normalised:
            # Exact hits all contain the identifier; keep their similarity order but compress it near the top.
            normalised["exact"] = {k: 0.6 + 0.4 * v for k, v in normalised["exact"].items()}
        boosts = plan.boosts
        for cand in candidates.values():
            score = sum(weights.get(leg, 0.0) * normalised[leg].get(cand.id, 0.0) for leg in legs)
            if boosts.get("doc_types") and cand.payload.get("doc_type") in boosts["doc_types"]:
                score += BOOST
            cand.fused = score

    def _demote_families(self, ranked: list[Candidate]) -> list[Candidate]:
        """Near-duplicate documents (same family) add nothing: keep one document per family on top.

        Family members are near-copies of one document, so their score differences are mostly noise: among members
        scoring at least half of the best, the representative is the one filed under a project and carrying a
        document number (the register copy rather than a stray duplicate in an archive folder)."""
        best: dict[str, dict[str, tuple[float, Candidate]]] = defaultdict(dict)
        for cand in ranked:
            family = cand.payload.get("family_id") or cand.document_id
            docs = best[family]
            if cand.document_id not in docs or cand.fused > docs[cand.document_id][0]:
                docs[cand.document_id] = (cand.fused, cand)
        owner: dict[str, str] = {}
        for family, docs in best.items():
            top = max(score for score, _ in docs.values())
            close = [(score, cand) for score, cand in docs.values() if score >= 0.5 * top]
            _, chosen = max(close, key=lambda sc: (bool(sc[1].payload.get("project_ids")),
                                                   bool(sc[1].payload.get("doc_number")), sc[0]))
            owner[family] = chosen.document_id
        for cand in ranked:
            family = cand.payload.get("family_id") or cand.document_id
            if owner.get(family, cand.document_id) != cand.document_id:
                cand.fused *= self.settings.family_demotion
                cand.demoted = True
        return sorted(ranked, key=lambda c: c.fused, reverse=True)

    async def _hydrate(self, cands: list[Candidate]) -> None:
        """Fill in header and text for candidates whose index payload does not carry them (STORE_TEXT_IN_INDEX=false): one
        query for the whole group, by the (version, chunk number) key the chunks table is organised by."""
        missing = [c for c in cands if "text" not in c.payload]
        if not missing:
            return
        async with transaction() as conn:
            rows = (await conn.execute(text(
                "SELECT version_id::text, chunk_no, header, text FROM chunks WHERE (version_id, chunk_no) IN "
                "(SELECT * FROM unnest(CAST(:v AS uuid[]), CAST(:n AS int[])))"),
                {"v": [c.payload.get("version_id") for c in missing],
                 "n": [int(c.payload.get("chunk_no", 0)) for c in missing]})).all()
        found = {(r[0], r[1]): (r[2], r[3]) for r in rows}
        for cand in missing:
            header, body = found.get((cand.payload.get("version_id"), int(cand.payload.get("chunk_no", 0))), ("", ""))
            cand.payload["header"], cand.payload["text"] = header, body

    def _name_boost(self, plan: QueryPlan, ranked: list[Candidate]) -> list[Candidate]:
        """Lift every chunk of a file whose name the question says ("from the order sheet" -> Order Sheet.xlsx).

        The words of the file name, in order, must appear in the question; one-word names count only when long enough
        to be a name rather than a common word."""
        boost = self.settings.name_boost
        words = _WORDS.findall(plan.query.lower())
        if boost <= 0 or not words:
            return ranked
        asked = {tuple(words[i : i + n]) for n in (1, 2, 3, 4) for i in range(len(words) - n + 1)}
        names: dict[str, bool] = {}
        hit = False
        for cand in ranked:
            name = str(cand.payload.get("file_name", ""))
            if name not in names:
                stem = _WORDS.findall(name.rsplit(".", 1)[0].lower())
                names[name] = (
                    bool(stem)
                    and tuple(stem) in asked
                    and (len(stem) > 1 or len(stem[0]) >= 6)
                    and not all(w in STOP_EN for w in stem)
                )
            if names[name]:
                cand.fused += boost
                cand.payload["_named"] = True
                hit = True
        return sorted(ranked, key=lambda c: c.fused, reverse=True) if hit else ranked

    def _phrase_boost(self, plan: QueryPlan, ranked: list[Candidate]) -> list[Candidate]:
        """Add to each top candidate the share of the question's word pairs it contains, adjacent and in order. Keyword
        search treats a question as a bag of words; a copied or lightly changed phrase is far stronger evidence than the
        same words scattered over a page."""
        boost = self.settings.phrase_boost
        words = _WORDS.findall(plan.query.lower())
        if boost <= 0 or len(words) < 4:
            return ranked
        pairs = {pair for pair in zip(words, words[1:], strict=False) if not (pair[0] in STOP_EN and pair[1] in STOP_EN)}
        if not pairs:
            return ranked
        window = max(self.settings.rerank_top_n, 40)
        for cand in ranked[:window]:
            body = _WORDS.findall(f"{cand.payload.get('header', '')} {cand.payload.get('text', '')}".lower())
            share = len(pairs & set(zip(body, body[1:], strict=False))) / len(pairs)
            cand.phrase = share
            cand.fused += boost * share
        return sorted(ranked, key=lambda c: c.fused, reverse=True)

    def _rerank_wanted(self, plan: QueryPlan) -> bool:
        """A cross-encoder reads questions; it reads keyword strings and identifier lookups badly (measured: used alone it
        lowers the first-rank rate). Short questions and questions naming an identifier go through the fused order."""
        s = self.settings
        if len(plan.original.split()) < s.rerank_min_words:
            return False
        return not (s.rerank_skip_identifiers and plan.identifiers)

    def _blend(self, top: list[Candidate]) -> float:
        """The cross-encoder's share of the final score for this question.

        Measured: a cross-encoder repairs paraphrases and tables (the words differ from the page, so keyword evidence is thin)
        but overrules exact evidence on questions that quote the page (it prefers a fluent neighbour). When some candidate
        already contains the question's word pairs, the fused order is trusted and the cross-encoder only breaks ties; when
        none does, it decides more."""
        s = self.settings
        ceiling = min(1.0, max(0.0, s.rerank_blend))
        if not s.rerank_adaptive:
            return ceiling
        floor = min(ceiling, max(0.0, s.rerank_blend_floor))
        quoted = min(1.0, max((c.phrase for c in top[:5]), default=0.0))
        return floor + (ceiling - floor) * (1.0 - quoted) ** max(0.1, s.rerank_adapt_power)

    async def _rerank_scores(self, reranker: Any, query: str, cands: list[Candidate],
                             info: dict[str, Any] | None = None) -> list[float]:
        """Cross-encoder scores, taking repeats (same question, same chunk text) from a cache: the score of a pair never
        changes, and the same question is asked again and again."""
        label = getattr(getattr(reranker, "spec", None), "label", "reranker")
        cache = self.services.__dict__.setdefault("_rerank_cache", OrderedDict())
        limit = max(0, self.settings.rerank_cache_size)
        keys = [(label, query, c.payload.get("content_hash") or c.id) for c in cands]
        out: list[float | None] = [cache.get(k) for k in keys]
        todo = [i for i, v in enumerate(out) if v is None]
        in_memory = len(cands) - len(todo)
        shared = get_cache(self.settings)
        qd = digest(query)
        shared_keys = [f"rr:{label}:{qd}:{k[2]}" for k in keys]
        if todo and shared.enabled:  # the other API processes (and earlier runs) may already have scored these pairs
            found = await shared.mget_floats([shared_keys[i] for i in todo])
            for i, score in zip(todo, found, strict=True):
                if score is not None:
                    out[i] = score
                    if limit:
                        cache[keys[i]] = score
            await shared.stat("rerank_hit", sum(1 for s in found if s is not None))
            await shared.stat("rerank_miss", sum(1 for s in found if s is None))
            todo = [i for i, v in enumerate(out) if v is None]
        if info is not None:
            info.update(model=label, pairs=len(cands), from_memory=in_memory,
                        from_shared_cache=len(cands) - in_memory - len(todo), scored_now=len(todo))
        if todo:
            texts = [f"{cands[i].payload.get('header', '')}\n{cands[i].payload.get('text', '')}"[:2000] for i in todo]
            for i, score in zip(todo, await reranker.rerank(query, texts), strict=True):
                out[i] = float(score)
                if limit:
                    cache[keys[i]] = out[i]
            if shared.enabled:
                await shared.mset_floats({shared_keys[i]: float(out[i]) for i in todo}, self.settings.cache_rerank_ttl_s)  # type: ignore[arg-type]
        while limit and len(cache) > limit:
            cache.popitem(last=False)
        return [float(v) for v in out]  # type: ignore[arg-type]

    async def _rerank(self, plan: QueryPlan, ranked: list[Candidate], info: dict[str, Any] | None = None) -> list[Candidate]:
        for c in ranked:
            c.final = c.fused
        info = {} if info is None else info
        reranker = self.services.hub.reranker()
        if reranker is None or not ranked or not self._rerank_wanted(plan):
            info["skipped"] = "no reranker" if reranker is None else "short or identifier question: fused order kept"
            return ranked
        top = ranked[: self.settings.rerank_top_n]
        rest = ranked[self.settings.rerank_top_n :]
        languages = getattr(reranker, "languages", None)
        query = plan.query
        if not languages_compatible(plan.language, languages):
            if plan.english:
                query = plan.english
            else:
                return ranked  # the reranker cannot read this question; keep the fused order
        eligible = [c for c in top if languages_compatible(c.payload.get("language", "und"), languages)]
        if len(eligible) < 2:
            return ranked
        try:
            scores = await self._rerank_scores(reranker, query, eligible, info)
        except Exception:
            log.warning("reranker failed; using fused scores", exc_info=True)
            info["skipped"] = "reranker failed: fused order kept"
            return ranked
        for cand, score in zip(eligible, scores, strict=True):
            cand.rerank = float(score)
        fused_scaled = _minmax({c.id: c.fused for c in top})
        rerank_scaled = _minmax({c.id: c.rerank for c in eligible if c.rerank is not None})
        blend = self._blend(top)
        info["blend"] = round(blend, 3)  # the cross-encoder's share of the final score
        for cand in top:
            if cand.rerank is not None:
                cand.final = blend * rerank_scaled[cand.id] + (1 - blend) * fused_scaled[cand.id]
            else:
                cand.final = fused_scaled[cand.id]
            if cand.demoted:
                cand.final *= self.settings.family_demotion
        floor = min((c.final for c in top), default=0.0)
        for cand in rest:
            cand.final = min(cand.fused, floor - 1e-6)
        return sorted(top, key=lambda c: c.final, reverse=True) + rest

    def _page_aggregate(self, ranked: list[Candidate]) -> list[Candidate]:
        """Lift a chunk by the best other chunk on the same page: a page where two chunks match is likelier to be the
        answer page than one where a single chunk scored a little higher. This is what makes the cited page right."""
        boost = self.settings.page_boost
        if boost <= 0 or len(ranked) < 2:
            return ranked
        by_page: dict[tuple[str, Any], list[Candidate]] = defaultdict(list)
        for cand in ranked:
            by_page[(cand.version_id, cand.payload.get("page_start"))].append(cand)
        lifted: dict[str, float] = {}
        for cands in by_page.values():
            if len(cands) < 2:
                continue
            cands.sort(key=lambda c: c.final, reverse=True)
            for index, cand in enumerate(cands):
                other = cands[1].final if index == 0 else cands[0].final
                lifted[cand.id] = boost * other
        for cand in ranked:
            cand.final += lifted.get(cand.id, 0.0)
        return sorted(ranked, key=lambda c: c.final, reverse=True)

    # ---- access re-check and documents ------------------------------------------------------------------------------
    async def _recheck(self, ranked: list[Candidate], principals: list[str], filters: SearchFilters) -> set[str]:
        version_ids = sorted({c.version_id for c in ranked if c.version_id})
        if not version_ids:
            return set()
        sql = (
            "SELECT v.id::text FROM versions v WHERE v.id = ANY(CAST(:ids AS uuid[])) AND v.state = 'live' "  # noqa: S608 - fixed fragments, bound values
            + "AND EXISTS (SELECT 1 FROM version_access a WHERE a.version_id = v.id "
            "AND a.principal = ANY(CAST(:p AS text[])))"
        )
        async with transaction() as conn:
            rows = (await conn.execute(text(sql), {"ids": version_ids, "p": principals})).all()
        allowed = {r[0] for r in rows}
        dropped = set(version_ids) - allowed
        if dropped:
            log.info("access re-check removed %s versions the index still listed", len(dropped))
        return allowed

    def _documents(self, ranked: list[Candidate]) -> list[DocumentHit]:
        grouped: dict[str, list[Candidate]] = defaultdict(list)
        for c in ranked:
            grouped[c.version_id].append(c)
        docs: list[DocumentHit] = []
        for version_id, cands in grouped.items():
            cands.sort(key=lambda c: c.final, reverse=True)
            p = cands[0].payload
            score = cands[0].final + (0.15 * cands[1].final if len(cands) > 1 else 0.0)
            docs.append(
                DocumentHit(
                    document_id=p.get("document_id", ""),
                    version_id=version_id,
                    score=round(score, 4),
                    doc_number=p.get("doc_number", ""),
                    title=p.get("title", ""),
                    file_name=p.get("file_name", ""),
                    project=(p.get("project_ids") or [""])[0],
                    doc_type=p.get("doc_type", ""),
                    pages=sorted({c.payload.get("page_start", 0) for c in cands})[:10],
                )
            )
        docs.sort(key=lambda d: d.score, reverse=True)
        return docs[: self.settings.candidate_docs]

    def _choose_passages(self, ranked: list[Candidate], limit: int) -> list[Candidate]:
        per_doc: dict[str, int] = defaultdict(int)
        families: set[str] = set()
        chosen: list[Candidate] = []
        min_score = self.settings.min_evidence_score
        for cand in ranked:
            if cand.final < min_score and chosen:
                break
            if per_doc[cand.version_id] >= self.settings.evidence_per_document:
                continue
            family = cand.payload.get("family_id") or cand.document_id
            if cand.demoted and family in families:
                continue  # a near-copy of a document already in the evidence adds nothing but noise
            per_doc[cand.version_id] += 1
            families.add(family)
            chosen.append(cand)
            if len(chosen) >= limit:
                break
        return chosen

    async def _expand(self, chosen: list[Candidate]) -> list[Passage]:
        """Small-to-big: add the neighbouring chunks on the same page and under the same heading so a passage reads as
        a whole. A neighbour under another heading (another level, tower or article) is a different fact, not context."""
        if not chosen:
            return []
        await self._hydrate(chosen)
        keys = [(c.version_id, int(c.payload.get("chunk_no", 0))) for c in chosen]
        neighbours: dict[tuple[str, int], tuple[str, int, int, str]] = {}
        async with transaction() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT version_id::text, chunk_no, text, page_start, token_count, section_path FROM chunks "
                        "WHERE (version_id, chunk_no) IN (SELECT * FROM unnest(CAST(:v AS uuid[]), CAST(:n AS int[])))"
                    ),
                    {"v": [k[0] for k in keys for _ in (0, 1)], "n": [k[1] + d for k in keys for d in (-1, 1)]},
                )
            ).all()
        for r in rows:
            neighbours[(r[0], r[1])] = (r[2], r[3], r[4], r[5])
        passages: list[Passage] = []
        for i, cand in enumerate(chosen, start=1):
            p = cand.payload
            core = p.get("text", "")
            page = int(p.get("page_start", 0))
            no = int(p.get("chunk_no", 0))
            before = neighbours.get((cand.version_id, no - 1))
            after = neighbours.get((cand.version_id, no + 1))
            section = p.get("section_path", "")
            parts = []
            if before and before[1] == page and before[2] < 200 and before[3] == section:
                parts.append(before[0])
            parts.append(core)
            if after and after[1] == page and after[2] < 200 and after[3] == section:
                parts.append(after[0])
            passages.append(
                Passage(
                    n=i,
                    chunk_id=cand.id,
                    version_id=cand.version_id,
                    document_id=cand.document_id,
                    doc_number=p.get("doc_number", ""),
                    title=p.get("title", ""),
                    page_start=page,
                    page_end=int(p.get("page_end", page)),
                    section_path=p.get("section_path", ""),
                    kind=p.get("kind", "text"),
                    text="\n".join(parts),
                    core_text=core,
                    score=round(cand.final, 4),
                    language=p.get("language", "und"),
                    file_name=p.get("file_name", ""),
                    project=(p.get("project_ids") or [""])[0],
                    doc_type=p.get("doc_type", ""),
                    local_only=bool(p.get("local_only")),
                )
            )
        return passages
