"""Choose the thresholds of the "nothing in the library answers this" gate from measurements, without a language model.

  python /eval/gate_calibrate.py ANSWERABLE.jsonl UNANSWERABLE.jsonl [allowed_loss ...]

Every question is run through retrieval with the shipped settings. The gate turns a question away when ALL of its enabled signals
(best cosine, best keyword score per term, best word coverage) are below their thresholds, so the search is over triples. The
unanswerable questions are split in two by position: thresholds are chosen on the first half and reported on the second half
too, so the numbers are not just the fit to the examples that produced them.

For each allowed loss (the share of answerable questions the gate may wrongly turn away) the table gives the thresholds, the
share of unanswerable questions caught in the fitting half and in the held-out half, and the same split by kind
(u-off = nothing like it in the library, u-near = shares vocabulary with it).
"""

from __future__ import annotations

import asyncio
import itertools
import os
import pickle
import re
import sys
from collections import OrderedDict
from pathlib import Path

from rag.config.settings import get_settings
from rag.evaluation import retrieval_bench as bench
from rag.jobs.handlers import load_handlers
from rag.services import Services


def quantiles(values: list[float], n: int = 14) -> list[float]:
    values = sorted(values)
    if not values:
        return [0.0]
    picks = {values[min(len(values) - 1, int(i * len(values) / n))] for i in range(n)}
    return sorted(picks | {0.0})


async def main(answerable: str, unanswerable: str, losses: list[float]) -> None:
    from blend_sweep import principals_of

    load_handlers()
    settings = get_settings()
    services = Services.create(settings)
    cache = services.__dict__.setdefault("_rerank_cache", OrderedDict())
    cache_file = Path(os.environ.get("RERANK_CACHE_DIR", "/devdeps/rerank_cache")) / f"rerank_{re.sub(r'[^A-Za-z0-9]+', '_', settings.reranker.replace('local/', 'local/'))}.pkl"
    if cache_file.exists():
        cache.update(pickle.loads(cache_file.read_bytes()))
    settings.rerank_cache_size = 1_000_000
    principals = await principals_of(os.environ.get("BENCH_AS", "admin@example.com"))
    items = [q for q in bench.load_questions(answerable) if not q.get("not_found")]
    nf = [q for q in bench.load_questions(unanswerable) if q.get("not_found")]
    nf += [q for q in bench.load_questions(answerable) if q.get("not_found")]
    seen: set[str] = set()
    nf = [q for q in nf if not (q["q"] in seen or seen.add(q["q"]))]
    try:
        rows, _ = await bench.run(services, items + nf, principals, rerank=settings.reranker != "none")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(pickle.dumps(dict(cache)))
    finally:
        await services.aclose()

    answerable_rows = [r for r in rows if not r["item"].get("not_found") and r.get("signals")]
    unanswerable_rows = [r for r in rows if r["item"].get("not_found") and r.get("signals")]
    fit = unanswerable_rows[0::2]
    held = unanswerable_rows[1::2]
    print(f"{len(answerable_rows)} answerable, {len(unanswerable_rows)} unanswerable ({len(fit)} fit / {len(held)} held out)")

    def sig(name: str, subset: list[dict]) -> list[float]:
        return [float(r["signals"][name]) for r in subset if r["signals"].get(name) is not None]

    for name in ("dense", "sparse", "coverage", "rerank"):
        a, u = sorted(sig(name, answerable_rows)), sorted(sig(name, unanswerable_rows))
        pct = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))] if xs else float("nan")  # noqa: E731
        print(f"  {name:9} answerable p1/p5/p50 {pct(a, .01):.3f}/{pct(a, .05):.3f}/{pct(a, .5):.3f}   "
              f"unanswerable p50/p95/max {pct(u, .5):.3f}/{pct(u, .95):.3f}/{u[-1]:.3f}")

    print("\nsingle signals (threshold that keeps all but X of the answerable questions, and what it turns away):")
    for name in ("dense", "sparse", "coverage", "rerank"):
        a = sorted(sig(name, answerable_rows))
        if not a:
            continue
        cells = []
        for loss in (0.005, 0.01, 0.02, 0.03, 0.05):
            t = a[min(len(a) - 1, int(loss * len(a)))]
            kw = {"min_dense": 0.0, "min_sparse": 0.0, "min_coverage": 0.0, "min_rerank": 0.0, f"min_{name}": t}
            cells.append(f"{loss * 100:g}%: <{t:.3f} -> {bench.gate_effect(unanswerable_rows, **kw)['unanswerable_caught'] * 100:3.0f}% "
                         f"(lost {bench.gate_effect(answerable_rows, **kw)['answerable_lost'] * 100:.1f}%)")
        print(f"  {name:9} " + "   ".join(cells))

    print("\nrerank probability as the only signal: answerable questions turned away, by kind (and unanswerable caught)")
    kinds = sorted({r["item"].get("kind", "-") for r in answerable_rows})
    print(f"  {'threshold':>9} " + "".join(f"{k[:10]:>11}" for k in kinds) + f"{'ALL':>8}{'unans.':>8}{'off':>6}{'near':>6}")
    for t in (0.005, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2):
        kw = {"min_dense": 0.0, "min_sparse": 0.0, "min_coverage": 0.0, "min_rerank": t}
        by_kind = []
        for k in kinds:
            subset = [r for r in answerable_rows if r["item"].get("kind", "-") == k]
            reranked = sum(1 for r in subset if r["signals"].get("rerank") is not None)
            by_kind.append(f"{bench.gate_effect(subset, **kw)['answerable_lost'] * 100:5.1f}%/{reranked:<3}")
        print(f"  {t:>9} " + "".join(f"{c:>11}" for c in by_kind) + f"{bench.gate_effect(answerable_rows, **kw)['answerable_lost'] * 100:>7.1f}%"
              f"{bench.gate_effect(unanswerable_rows, **kw)['unanswerable_caught'] * 100:>7.0f}%"
              f"{bench.gate_effect([r for r in unanswerable_rows if str(r['item'].get('id', '')).startswith('u-off')], **kw)['unanswerable_caught'] * 100:>5.0f}%"
              f"{bench.gate_effect([r for r in unanswerable_rows if str(r['item'].get('id', '')).startswith('u-near')], **kw)['unanswerable_caught'] * 100:>5.0f}%")
    print("  (each kind cell: share turned away / how many of that kind were reranked and so could be)")

    grid = {name: quantiles(sig(name, answerable_rows), 9) for name in ("dense", "sparse", "coverage", "rerank")}

    def effect(rows_: list[dict], d: float, s: float, c: float, r: float) -> float:
        return bench.gate_effect(rows_, min_dense=d, min_sparse=s, min_coverage=c, min_rerank=r)["unanswerable_caught"]

    def lost(d: float, s: float, c: float, r: float) -> float:
        return bench.gate_effect(answerable_rows, min_dense=d, min_sparse=s, min_coverage=c, min_rerank=r)["answerable_lost"]

    candidates = []
    for d, s, c, r in itertools.product(grid["dense"], grid["sparse"], grid["coverage"], grid["rerank"]):
        if d == s == c == r == 0:
            continue
        candidates.append((d, s, c, r, lost(d, s, c, r), effect(fit, d, s, c, r)))
    print(f"\n{'allowed loss':>12} {'dense':>7} {'sparse':>7} {'cover':>7} {'rerank':>8} {'lost':>6} {'caught fit':>11} {'held out':>9} {'off':>6} {'near':>6}")
    off = [r for r in unanswerable_rows if str(r["item"].get("id", "")).startswith("u-off")]
    near = [r for r in unanswerable_rows if str(r["item"].get("id", "")).startswith("u-near")]
    for allowed in losses:
        ok = [x for x in candidates if x[4] <= allowed]
        if not ok:
            print(f"{allowed * 100:>11.1f}%  no setting keeps the loss that low")
            continue
        best = max(ok, key=lambda x: (x[5], -x[4], x[0] + x[1] + x[2] + x[3]))
        d, s, c, r, lo, caught = best
        print(f"{allowed * 100:>11.1f}% {d:>7.3f} {s:>7.3f} {c:>7.3f} {r:>8.4f} {lo * 100:>5.1f}% {caught * 100:>10.1f}% "
              f"{effect(held, d, s, c, r) * 100:>8.1f}% {effect(off, d, s, c, r) * 100:>5.0f}% {effect(near, d, s, c, r) * 100:>5.0f}%")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], [float(x) for x in sys.argv[3:]] or [0.0, 0.005, 0.01, 0.02]))
