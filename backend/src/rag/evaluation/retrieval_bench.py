"""Retrieval benchmark: does the right document, and the right PAGE, come back on top? No language model involved.

A question set is a JSONL file, one object per line:

    {"id": "nist-1", "q": "which control requires multifactor authentication for privileged accounts",
     "kind": "paraphrase", "file": "NIST-SP.800-171r3", "pages": [23, 24]}
    {"id": "nf-1", "q": "warranty period of the passenger lifts", "kind": "unanswerable", "not_found": true}

  file   substring of the expected file name (or a list of alternatives)
  pages  pages that hold the answer (optional; without it only the document is scored)
  kind   free label, used to split the report (exact, keyword, paraphrase, table, identifier, drawing, arabic ...)

Reported, overall and per kind:
  doc_hit@k    the expected document is within the first k documents
  page_hit@k   an expected page is within the first k ranked chunks (the retrieval itself, before evidence selection)
  cite@k       an expected page is within the first k evidence passages (what the answer, and its citation, is built from)
  mrr_doc, mrr_page, and latency percentiles.
For `not_found` questions the best score is recorded, so a refusal threshold can be chosen from real numbers.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from rag.retrieval.query_router import QueryRouter
from rag.retrieval.retriever import Retriever
from rag.retrieval.types import SearchFilters

DOC_K = (1, 3, 5, 10)
PAGE_K = (1, 3, 5, 10, 20)
CITE_K = (1, 3, 5)


def load_questions(path: str | Path) -> list[dict[str, Any]]:
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(json.loads(line))
    return items


def _files(item: dict[str, Any]) -> list[str]:
    value = item.get("file") or []
    return [v.lower() for v in ([value] if isinstance(value, str) else value)]


def _file_ok(file_name: str, wanted: list[str]) -> bool:
    name = (file_name or "").lower()
    return any(w in name for w in wanted)


def _page_ok(start: int, end: int, pages: list[int]) -> bool:
    return not pages or any(start <= p <= end for p in pages)


def _first(flags: list[bool]) -> int | None:
    return next((i + 1 for i, ok in enumerate(flags) if ok), None)


def score_item(item: dict[str, Any], result: Any) -> dict[str, Any]:
    wanted, pages = _files(item), [int(p) for p in item.get("pages") or []]
    ranked = (result.debug or {}).get("ranked", [])
    out: dict[str, Any] = {}
    if item.get("not_found"):
        out["top_final"] = ranked[0]["final"] if ranked else 0.0
        out["top_fused"] = ranked[0]["fused"] if ranked else 0.0
        return out
    doc_rank = _first([_file_ok(d.file_name, wanted) for d in result.documents])
    chunk_rank = _first([_file_ok(c["file_name"], wanted) and _page_ok(c["page"], c["page_end"], pages)
                         for c in ranked])
    cite_rank = _first([_file_ok(p.file_name, wanted) and _page_ok(p.page_start, p.page_end, pages)
                        for p in result.passages])
    out.update(doc_rank=doc_rank, page_rank=chunk_rank, cite_rank=cite_rank)
    return out


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def block(subset: list[dict[str, Any]]) -> dict[str, Any]:
        answerable = [r for r in subset if not r["item"].get("not_found")]
        with_pages = [r for r in answerable if r["item"].get("pages")]
        out: dict[str, Any] = {"n": len(subset)}
        if answerable:
            n = len(answerable)
            for k in DOC_K:
                out[f"doc_hit@{k}"] = round(sum(1 for r in answerable if r["doc_rank"] and r["doc_rank"] <= k) / n, 4)
            out["mrr_doc"] = round(sum(1 / r["doc_rank"] for r in answerable if r["doc_rank"]) / n, 4)
        if with_pages:
            n = len(with_pages)
            for k in PAGE_K:
                out[f"page_hit@{k}"] = round(sum(1 for r in with_pages if r["page_rank"] and r["page_rank"] <= k) / n, 4)
            for k in CITE_K:
                out[f"cite@{k}"] = round(sum(1 for r in with_pages if r["cite_rank"] and r["cite_rank"] <= k) / n, 4)
            out["mrr_page"] = round(sum(1 / r["page_rank"] for r in with_pages if r["page_rank"]) / n, 4)
            out["n_paged"] = n
        return out

    summary: dict[str, Any] = {"overall": block(rows), "by_kind": {}}
    for kind in sorted({r["item"].get("kind", "-") for r in rows}):
        summary["by_kind"][kind] = block([r for r in rows if r["item"].get("kind", "-") == kind])
    latencies = [r["latency_ms"] for r in rows]
    if latencies:
        summary["latency_p50_ms"] = round(statistics.median(latencies), 1)
        summary["latency_p95_ms"] = round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1)
    refused = [r["top_final"] for r in rows if r["item"].get("not_found") and "top_final" in r]
    answered = [r["page_score"] for r in rows if r.get("page_score") is not None]
    if refused:
        summary["not_found_top_final"] = {"n": len(refused), "max": round(max(refused), 4),
                                          "median": round(statistics.median(refused), 4)}
    if answered:
        summary["answerable_top_final"] = {"n": len(answered), "min": round(min(answered), 4),
                                           "median": round(statistics.median(answered), 4)}
    cal = calibration(rows)
    if cal:
        summary["calibration"] = cal
    return summary


SIGNALS = ("dense", "sparse", "coverage", "rerank")


def calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How well each relevance signal separates answerable from unanswerable questions, and the threshold that keeps all
    but `loss` of the answerable ones. auc = the chance a random answerable question scores above a random unanswerable
    one (1.0 perfect, 0.5 no information)."""
    answerable = [r["signals"] for r in rows if not r["item"].get("not_found") and r.get("signals") and r.get("doc_rank")]
    unanswerable = [r["signals"] for r in rows if r["item"].get("not_found") and r.get("signals")]
    if len(answerable) < 5 or len(unanswerable) < 3:
        return {}
    out: dict[str, Any] = {"answerable": len(answerable), "unanswerable": len(unanswerable)}
    for name in SIGNALS:
        a = sorted(float(x[name]) for x in answerable if x.get(name) is not None)
        u = sorted(float(x[name]) for x in unanswerable if x.get(name) is not None)
        if not a or not u:
            continue
        wins = sum((1.0 if x > y else 0.5 if x == y else 0.0) for x in a for y in u) / (len(a) * len(u))
        entry: dict[str, Any] = {"auc": round(wins, 3)}
        for loss in (0.01, 0.03, 0.05):
            threshold = a[min(len(a) - 1, int(loss * len(a)))]
            entry[f"at_{int(loss * 100)}pct_loss"] = {
                "threshold": round(threshold, 4), "unanswerable_turned_away": round(sum(1 for y in u if y < threshold) / len(u), 3)}
        out[name] = entry
    return out


def gate_effect(rows: list[dict[str, Any]], *, min_dense: float, min_sparse: float, min_coverage: float,
                min_rerank: float = 0.0) -> dict[str, float]:
    """What a set of thresholds would have done to these questions: the share of answerable questions it would wrongly turn
    away and the share of unanswerable ones it would catch."""
    from rag.retrieval import relevance

    def off(sig: dict[str, Any]) -> bool:
        return relevance.is_off_topic(
            relevance.Signals(dense=sig.get("dense"), sparse=sig.get("sparse"), coverage=sig.get("coverage") or 0.0,
                              rerank=sig.get("rerank"), identifier_found=bool(sig.get("identifier_found"))),
            min_dense=min_dense, min_sparse=min_sparse, min_coverage=min_coverage, min_rerank=min_rerank)

    a = [r for r in rows if not r["item"].get("not_found") and r.get("signals")]
    u = [r for r in rows if r["item"].get("not_found") and r.get("signals")]
    return {"answerable_lost": round(sum(1 for r in a if off(r["signals"])) / max(1, len(a)), 4),
            "unanswerable_caught": round(sum(1 for r in u if off(r["signals"])) / max(1, len(u)), 4)}


async def run(services: Any, items: list[dict[str, Any]], principals: list[str], *, rerank: bool = True
              ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    retriever = Retriever(services)
    router = QueryRouter(known_projects=await services.known_projects())
    rows: list[dict[str, Any]] = []
    for item in items:
        started = time.perf_counter()
        plan = router.rules(item["q"], SearchFilters.from_dict(item.get("filters")))
        result = await retriever.search(plan, principals, rerank=rerank, debug=True)
        latency = (time.perf_counter() - started) * 1000
        row = {"item": item, "latency_ms": round(latency, 1), **score_item(item, result)}
        ranked = result.debug.get("ranked", [])
        if not item.get("not_found") and ranked:
            row["page_score"] = ranked[0]["final"]
        row["top"] = [{"file": c["file_name"], "page": c["page"], "final": c["final"]} for c in ranked[:5]]
        row["signals"] = result.signals
        rows.append(row)
    return rows, summarise(rows)


_COLUMNS = (("n", "n"), ("doc_hit@1", "doc@1"), ("doc_hit@5", "doc@5"), ("page_hit@1", "pg@1"), ("page_hit@3", "pg@3"),
            ("page_hit@5", "pg@5"), ("page_hit@10", "pg@10"), ("cite@1", "cite@1"), ("cite@3", "cite@3"),
            ("mrr_page", "mrrP"))


def format_table(summary: dict[str, Any]) -> str:
    """A fixed-width report: one row for everything, one per question kind."""
    def cell(block: dict[str, Any], key: str) -> str:
        value = block.get(key)
        if value is None:
            return "-"
        return str(value) if key == "n" else f"{value * 100:.1f}" if key != "mrr_page" else f"{value:.3f}"

    lines = ["kind".ljust(14) + "".join(label.rjust(8) for _, label in _COLUMNS)]
    for name, block in [("ALL", summary["overall"]), *summary["by_kind"].items()]:
        lines.append(name[:13].ljust(14) + "".join(cell(block, key).rjust(8) for key, _ in _COLUMNS))
    tail = [f"latency p50 {summary.get('latency_p50_ms')} ms, p95 {summary.get('latency_p95_ms')} ms"]
    cal = summary.get("calibration")
    if cal:
        tail.append(f"relevance signals ({cal['answerable']} answerable, {cal['unanswerable']} unanswerable questions):")
        for name in SIGNALS:
            if name in cal:
                e = cal[name]
                tail.append(f"  {name:9} auc {e['auc']:.3f}  | at 1% loss: <{e['at_1pct_loss']['threshold']} turns away "
                            f"{e['at_1pct_loss']['unanswerable_turned_away'] * 100:.0f}%  | 3%: <{e['at_3pct_loss']['threshold']} "
                            f"{e['at_3pct_loss']['unanswerable_turned_away'] * 100:.0f}%  | 5%: <{e['at_5pct_loss']['threshold']} "
                            f"{e['at_5pct_loss']['unanswerable_turned_away'] * 100:.0f}%")
    if "not_found_top_final" in summary:
        tail.append(f"unanswerable top score {summary['not_found_top_final']}, answerable top score "
                    f"{summary.get('answerable_top_final')}")
    return "\n".join(lines + tail)
