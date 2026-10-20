"""Full answer (calls the answer model): python /eval/ask_full.py "question". Prints the steps, the answer and the model usage."""
from __future__ import annotations

import asyncio
import os
import sys

from rag.answering.agent import Answerer
from rag.config.settings import get_settings
from rag.jobs.handlers import load_handlers
from rag.services import Services


async def main(question: str) -> None:
    from blend_sweep import principals_of

    load_handlers()
    services = Services.create(get_settings())
    principals = await principals_of(os.environ.get("BENCH_AS", "admin@example.com"))
    text = ""
    async for ev in Answerer(services).answer(question, principals=principals):
        name, data = ev["event"], ev["data"]
        if name == "status":
            print("  *", data.get("message"))
        elif name == "delta":
            text += data["text"]
        elif name == "done":
            print("  steps/usage:", data["usage"], "status:", data["status"], "ms:", data["timings_ms"].get("total"))
        elif name == "error":
            print("  ERROR", data)
    print(text)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
