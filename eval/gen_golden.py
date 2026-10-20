"""Generate known-item retrieval questions from the pages already stored in Postgres (no language model).

For every document it picks a few pages, finds sentences that occur only on that page of the whole library, and turns
each into questions whose true answer page is known:

  exact    a 9-14 word stretch copied from the sentence
  keyword  the sentence's 5 rarest words (by page frequency), in their original order
  noisy    the sentence with some words dropped and a typo or two, the way people type

Run inside the API container:  python /eval/gen_golden.py --per-doc 3 --out /tmp/golden_auto.jsonl
These test whether the page can be FOUND from its own words; paraphrase questions (hand written) test meaning.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from collections import Counter

import psycopg

from rag.config.settings import get_settings

WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")
SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
STOP = set("the a an and or of to in on for with is are was were be been by as at from that this these those it its their his her which who whom "
           "has have had not but if than then also can may will would should could into over under between about such each other more most some any all".split())
BAD = re.compile(r"(et al|\bfig\b|\btable\b|https?:|www\.|\bpp\.|©|\[\d+|\bvol\.|\bdoi\b|\(c\)|\.{4,}|\bcopyright\b)", re.I)


def shingles(words: list[str], n: int = 5) -> list[int]:
    return [hash(" ".join(words[i:i + n])) for i in range(len(words) - n + 1)]


def typo(word: str, rng: random.Random) -> str:
    if len(word) < 5:
        return word
    i = rng.randrange(1, len(word) - 2)
    kind = rng.choice(["swap", "drop", "dup"])
    if kind == "swap":
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    if kind == "drop":
        return word[:i] + word[i + 1:]
    return word[:i] + word[i] + word[i:]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-doc", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--source", default="eval-%", help="source-name pattern")
    ap.add_argument("--out", default="/tmp/golden_auto.jsonl")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    s = get_settings()
    dsn = s.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as c:
        docs = c.execute(
            "SELECT i.name, b.sha256, b.page_count, so.name FROM source_items i JOIN blobs b ON b.sha256=i.content_sha256 "
            "JOIN sources so ON so.id=i.source_id WHERE so.name LIKE %s AND i.status='active' AND b.page_count>=3 "
            "ORDER BY i.name", (args.source,)).fetchall()
        # candidate sentences
        cands = []
        for name, sha, n_pages, group in docs:
            rows = c.execute("SELECT page_no, text FROM pages WHERE blob_sha256=%s AND length(text)>800 ORDER BY page_no", (sha,)).fetchall()
            rows = [r for r in rows if 1 < r[0] < n_pages] or rows
            rng.shuffle(rows)
            taken = 0
            for page_no, text in rows:
                if taken >= args.per_doc:
                    break
                sents = [x.strip().replace("\n", " ") for x in SENT.split(text)]
                rng.shuffle(sents)
                for sent in sents:
                    words = WORD.findall(sent)
                    if not (14 <= len(words) <= 38) or len(sent) > 330 or BAD.search(sent):
                        continue
                    if sum(ch.isalpha() for ch in sent) / max(len(sent), 1) < 0.78:
                        continue
                    cands.append({"name": name, "group": group, "page": page_no, "sent": sent, "words": [w.lower() for w in words]})
                    taken += 1
                    break
        # which of those shingles / words occur elsewhere in the library?
        wanted_sh = {h for cd in cands for h in shingles(cd["words"])}
        wanted_words = {w for cd in cands for w in cd["words"]}
        sh_pages: dict[int, set] = {}
        df: Counter = Counter()
        total_pages = 0
        for sha, page_no, text in c.execute("SELECT p.blob_sha256, p.page_no, p.text FROM pages p JOIN source_items i ON i.content_sha256=p.blob_sha256 JOIN sources so ON so.id=i.source_id WHERE so.name LIKE %s", (args.source,)):
            total_pages += 1
            ws = [w.lower() for w in WORD.findall(text)]
            for w in set(ws) & wanted_words:
                df[w] += 1
            for h in set(shingles(ws)) & wanted_sh:
                sh_pages.setdefault(h, set()).add((sha, page_no))
    out = []
    for i, cd in enumerate(cands):
        sh = shingles(cd["words"])
        shared = sum(1 for h in sh if len(sh_pages.get(h, ())) > 1)
        if sh and shared / len(sh) > 0.2:
            continue  # the same wording is printed on other pages: the "right page" would be ambiguous
        words = cd["words"]
        base = {"file": cd["name"], "pages": [cd["page"]], "group": cd["group"]}
        n = rng.randint(9, 14)
        start = rng.randint(0, max(0, len(words) - n))
        out.append({"id": f"a{i}-exact", "q": " ".join(words[start:start + n]), "kind": "exact", **base})
        idf = {w: math.log((total_pages + 1) / (df[w] + 1)) for w in set(words) if w not in STOP and len(w) > 3}
        top = sorted(idf, key=idf.get, reverse=True)[:5]
        out.append({"id": f"a{i}-kw", "q": " ".join(w for w in words if w in top and not top.remove(w)), "kind": "keyword", **base})
        kept = [w for w in words if w in STOP or rng.random() > 0.35]
        noisy = " ".join(typo(w, rng) if (w not in STOP and rng.random() < 0.25) else w for w in kept)
        out.append({"id": f"a{i}-noisy", "q": noisy, "kind": "noisy", **base})
    with open(args.out, "w", encoding="utf-8") as f:
        for item in out:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"{len(docs)} documents, {total_pages} pages, {len(cands)} candidate sentences, {len(out)} questions -> {args.out}")


if __name__ == "__main__":
    main()
