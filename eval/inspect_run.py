"""The Details panel's items for one question, without the browser: python /eval/inspect_run.py "question"."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from rag.answering.agent import Answerer
from rag.config.settings import get_settings
from rag.jobs.handlers import load_handlers
from rag.monitoring import inspector
from rag.services import Services


async def main(question: str) -> None:
    from blend_sweep import principals_of

    load_handlers()
    services = Services.create(get_settings())
    principals = await principals_of(os.environ.get("BENCH_AS") or os.environ["BOOTSTRAP_ADMIN_EMAIL"])
    items: dict[str, dict] = {}
    order: list[str] = []

    def emit(item: dict) -> None:
        if item["id"] not in items:
            order.append(item["id"])
        items[item["id"]] = json.loads(json.dumps(item, default=str))

    inspector.activate(inspector.Inspector(emit))
    async for _ev in Answerer(services).answer(question, principals=principals):
        pass
    size = 0
    for key in order:
        item = items[key]
        size += len(json.dumps(item))
        data = item["data"]
        extra = ""
        if item["kind"] == "llm":
            extra = (f" in={data.get('input_tokens')} out={data.get('output_tokens')} msgs={len(data['messages'])} "
                     f"sent={data['sent_chars']} masked={data.get('masked_values')} reply={len(data.get('reply') or '')}")
        elif item["kind"] == "search":
            extra = f" cands={len(data['candidates'])} passages={len(data['passages'])} rerank={data.get('rerank')}"
        elif item["kind"] == "embed":
            extra = f" dims={data['dims']} source={data['source']}"
        print(f"{item['at_ms']:>8.0f} ms  {item['kind']:<11} {item['status']:<7} {item['title'][:90]}{extra}")
    print(f"items: {len(order)}, total JSON: {size:,} bytes")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
