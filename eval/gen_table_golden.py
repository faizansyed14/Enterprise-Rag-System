"""Questions about table cells, generated from the labelled table rows already stored (no language model).

A table chunk holds rows such as  `Paper: Kim and Choi (2018) | Domain: CV | Tasks: classification | Method: SPL`.
For a row whose first cell occurs nowhere else in the library the question asks for two other columns by name:

    "What are the Domain and Method for Kim and Choi (2018)?"

so it can only be answered from the right row of the right page; it tests that column names reached the rows (including on
pages where a table continues without repeating its heading) and that word spacing survived extraction.

  python /eval/gen_table_golden.py --max 80 --out /tmp/golden_tables.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict

import psycopg

from rag.config.settings import get_settings

CELL = re.compile(r"^([^:|]{2,40}): (.+)$")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=80)
    parser.add_argument("--per-doc", type=int, default=6)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    dsn = get_settings().database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as c:
        rows = c.execute(
            "SELECT i.name, c.page_start, c.page_end, c.text FROM chunks c JOIN versions v ON v.id = c.version_id "
            "JOIN source_items i ON i.content_sha256 = v.blob_sha256 AND i.status = 'active' "
            "WHERE c.kind = 'table' AND v.state = 'live'").fetchall()
        everything = " \n".join(r[0] for r in c.execute("SELECT text FROM chunks").fetchall())
    rng = random.Random(7)
    by_doc: dict[str, list[tuple[int, int, list[tuple[str, str]]]]] = defaultdict(list)
    for name, p0, p1, text in rows:
        for line in text.split("\n"):
            cells = [CELL.match(part.strip()) for part in line.split(" | ")]
            cells = [m for m in cells if m]
            if len(cells) < 4:
                continue
            pairs = [(m.group(1).strip(), m.group(2).strip()) for m in cells]
            key = pairs[0][1]
            if len(key) < 6 or everything.count(key) != 1:
                continue
            others = [(k, v) for k, v in pairs[1:] if 2 <= len(v) <= 60 and k.lower() != pairs[0][0].lower()]
            if len(others) >= 2:
                by_doc[name].append((p0, p1, pairs))
    items = []
    for name, found in sorted(by_doc.items()):
        for p0, p1, pairs in rng.sample(found, min(args.per_doc, len(found))):
            others = [(k, v) for k, v in pairs[1:] if 2 <= len(v) <= 60]
            a, b = rng.sample(others, 2)
            stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name).lower()
            items.append({"id": f"tbl-{len(items)}", "q": f"What are the {a[0]} and {b[0]} for {pairs[0][1]}?", "kind": "table",
                          "file": stem, "pages": sorted({p0, p1})})
    rng.shuffle(items)
    items = items[: args.max]
    with open(args.out, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"{len(items)} questions from {len({i['file'] for i in items})} documents -> {args.out}")


if __name__ == "__main__":
    main()
