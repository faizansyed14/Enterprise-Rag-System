"""Everything that happens while one question is answered, sent live to the chat's Details panel.

Each step adds an item: the question check, the query plan, the question's embedding, every search with each candidate's
scores, the files read, every model call with the messages exactly as they left this server (after the privacy shield
masked them) and the reply as it came back, the evidence checks, the arithmetic and the citation checks. Items go out as
`trace` events next to the answer's own events; nothing is written to the database.

The inspector of the current request sits in a context variable, so the code that runs a step (the retriever, the
metering wrapper around every model) records it without being handed anything. Outside a chat request there is no
inspector and every call here does nothing.

  item         {"id", "seq", "at_ms", "kind", "title", "status": running | done | error, "ms", "data"}
  update       the same id sent again: a model call is added when it starts and completed when it returns
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import Any

TEXT_LIMIT = 120_000  # one message or document text; longer ones are cut with a note of how much was left out

_CURRENT: ContextVar[Inspector | None] = ContextVar("inspector", default=None)
_PURPOSE: ContextVar[str] = ContextVar("inspector_purpose", default="")
EMBED_SOURCE: ContextVar[str] = ContextVar("embed_source", default="")  # set by an embedder: memory | shared cache | model


def clip(value: Any, limit: int = TEXT_LIMIT) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… ({len(text) - limit:,} more characters not shown)"


class Inspector:
    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit = emit
        self._started = time.perf_counter()
        self._seq = itertools.count(1)
        self._items: dict[str, dict[str, Any]] = {}

    def _now(self) -> float:
        return round((time.perf_counter() - self._started) * 1000, 1)

    def add(self, kind: str, title: str, *, status: str = "done", ms: float | None = None, **data: Any) -> str:
        seq = next(self._seq)
        item = {"id": f"i{seq}", "seq": seq, "at_ms": self._now(), "kind": kind, "title": title, "status": status,
                "ms": None if ms is None else round(ms, 1), "data": data}
        self._items[item["id"]] = item
        self._emit(item)
        return item["id"]

    def finish(self, item_id: str, *, status: str = "done", title: str | None = None, **data: Any) -> None:
        item = self._items.get(item_id)
        if item is None:
            return
        item["status"] = status
        item["ms"] = round(self._now() - item["at_ms"], 1)
        if title:
            item["title"] = title
        item["data"] = {**item["data"], **data}
        self._emit(item)


def current() -> Inspector | None:
    return _CURRENT.get()


def activate(inspector: Inspector | None) -> Any:
    return _CURRENT.set(inspector)


def note(kind: str, title: str, *, ms: float | None = None, **data: Any) -> str | None:
    """Add a finished step to the current request's details (nothing when there is no inspector)."""
    inspector = _CURRENT.get()
    return inspector.add(kind, title, ms=ms, **data) if inspector is not None else None


@contextmanager
def purpose(label: str) -> Iterator[None]:
    """Name the model calls made inside the block ("Planning how to answer"), so the panel says why each call was made."""
    token = _PURPOSE.set(label)
    try:
        yield
    finally:
        with suppress(ValueError):  # a streaming answer closed from another context: the label dies with that context
            _PURPOSE.reset(token)


# ---- model calls (recorded by the metering wrapper, which sees the masked request) -------------------------------------
def _message(m: Any) -> dict[str, Any]:
    content = m.content
    if not isinstance(content, str):
        content = "\n".join("[image]" if part.get("type") == "image" else str(part.get("text", "")) for part in content)
    out: dict[str, Any] = {"role": m.role, "content": clip(content), "chars": len(content)}
    if getattr(m, "tool_calls", None):
        out["tool_calls"] = [{"name": c.name, "arguments": c.arguments} for c in m.tool_calls]
    if getattr(m, "name", None):
        out["name"] = m.name
    return out


def call_started(spec: Any, messages: Sequence[Any], params: dict[str, Any], *, streamed: bool,
                 tools: Sequence[Any] | None = None, masked: int | None = None) -> str | None:
    inspector = _CURRENT.get()
    if inspector is None:
        return None
    why = _PURPOSE.get() or spec.role
    shown = {k: v for k, v in params.items() if k in {"max_tokens", "temperature", "json_mode", "timeout"}}
    return inspector.add(
        "llm", f"{why}: {spec.model}", status="running",
        purpose=why, role=spec.role, provider=spec.provider, model=spec.model, local=spec.is_local, streamed=streamed,
        params=shown, tools=[t.name for t in tools or []], masked_values=masked,
        messages=[_message(m) for m in messages], sent_chars=sum(len(str(m.content)) for m in messages),
    )


def call_finished(item_id: str | None, *, content: str = "", tool_calls: Sequence[Any] = (), usage: Any = None,
                  finish_reason: str | None = None, error: BaseException | None = None) -> None:
    inspector = _CURRENT.get()
    if inspector is None or item_id is None:
        return
    data: dict[str, Any] = {"reply": clip(content), "finish_reason": finish_reason,
                            "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in tool_calls]}
    if usage is not None:
        data.update(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cost_usd=usage.cost_usd)
    if error is not None:
        data["error"] = f"{type(error).__name__}: {error}"[:500]
    inspector.finish(item_id, status="error" if error is not None else "done", **data)


def embedded(spec: Any, text: str, vector: Sequence[float], elapsed_ms: float) -> None:
    inspector = _CURRENT.get()
    if inspector is None:
        return
    norm = math.sqrt(sum(v * v for v in vector)) if vector else 0.0
    source = EMBED_SOURCE.get() or "model"
    inspector.add("embed", f"Question embedded ({source})", ms=elapsed_ms, model=spec.model, provider=spec.provider,
                  text=text, dims=len(vector), norm=round(norm, 4), source=source,
                  preview=[round(float(v), 5) for v in vector[:16]])
