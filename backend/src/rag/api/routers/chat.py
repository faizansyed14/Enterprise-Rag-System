"""Chat (server-sent events), conversations and feedback."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from rag.answering.agent import Answerer, AnswerOutcome
from rag.api.deps import PrincipalsDep, ServicesDep, UserDep
from rag.api.sse import sse_response
from rag.db import schema
from rag.db.engine import transaction
from rag.monitoring import inspector
from rag.monitoring.recorder import get_recorder
from rag.monitoring.tracing import trace
from rag.plugins.hub import LOCAL_ANSWERER
from rag.providers import openrouter_catalog
from rag.retrieval.types import SearchFilters
from rag.security.rate_limit import RateLimitExceeded, hit

router = APIRouter(tags=["chat"])
C = schema.conversations.c
M = schema.messages.c


class ChatBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None


async def _conversation(user_id: UUID, conversation_id: UUID | None, question: str,
                        filters: dict[str, Any]) -> tuple[UUID, list[dict[str, str]]]:
    async with transaction() as conn:
        if conversation_id is not None:
            row = (await conn.execute(select(C.id).where(C.id == conversation_id, C.user_id == user_id))).first()
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
            history_rows = (await conn.execute(
                select(M.role, M.content).where(M.conversation_id == conversation_id)
                .order_by(M.created_at.desc()).limit(6))).all()
            history = [{"role": r.role, "content": r.content} for r in reversed(history_rows)]
        else:
            title = " ".join(question.split())[:80]
            conversation_id = (await conn.execute(
                schema.conversations.insert().values(user_id=user_id, title=title, filters=filters)
                .returning(C.id))).scalar_one()
            history = []
        await conn.execute(schema.messages.insert().values(conversation_id=conversation_id, role="user",
                                                           content=question, filters=filters))
        await conn.execute(update(schema.conversations).where(C.id == conversation_id)
                           .values(updated_at=datetime.now(UTC)))
    return conversation_id, history


@router.get("/chat/models")
async def chat_models(_: UserDep, services: ServicesDep, refresh: bool = False) -> dict:
    """What the model picker offers: the local answerer, the shortlist from MODEL_CHOICES, and (when allowed) every model in
    OpenRouter's public catalogue with its price per million tokens."""
    s = services.settings
    catalogue = (openrouter_catalog.curate(await openrouter_catalog.models(force=refresh), s.openrouter_models)
                 if s.allow_any_openrouter_model else [])
    return {"local": LOCAL_ANSWERER, "shortlist": s.model_choice_lines, "current": services.hub.spec("LLM_ANSWER").label,
            "openrouter": catalogue, "openrouter_enabled": bool(s.secret("openrouter_api_key"))}


@router.post("/chat")
async def chat(body: ChatBody, user: UserDep, principals: PrincipalsDep, services: ServicesDep):
    s = services.settings
    try:
        await hit(f"chat:{user.id}", s.rate_limit_chat_per_min, 60)
    except RateLimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many questions; slow down a little",
                            headers={"Retry-After": str(exc.retry_after)}) from exc
    if body.model and not s.allow_ui_model_switch and body.model != services.hub.spec("LLM_ANSWER").label:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Model choice is disabled")
    conversation_id, history = await _conversation(user.id, body.conversation_id, body.question, body.filters)
    filters = SearchFilters.from_dict(body.filters)

    details = s.chat_details == "all" or (s.chat_details == "admin" and user.is_admin)

    async def events() -> AsyncIterator[dict[str, Any]]:
        # The answer runs in its own task and every event (and every step of the Details panel, which can happen at any
        # moment inside a step, such as a model call starting) goes through one queue, so the browser sees it at once.
        outcome = AnswerOutcome()
        recorder = get_recorder()
        yield {"event": "conversation", "data": {"conversation_id": str(conversation_id)}}
        state: dict[str, Any] = {"message_id": None, "trace_id": None}
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def answer() -> None:
            inspector.activate(inspector.Inspector(lambda item: queue.put_nowait(("trace", item))) if details else None)
            try:
                with recorder.live.active("chat"):
                    async with trace("chat", "answer", user_id=str(user.id), conversation=str(conversation_id)) as t:
                        state["trace_id"] = t.id
                        async for event in Answerer(services).answer(
                                body.question, principals=principals, filters=filters, history=history,
                                model_choice=body.model, outcome=outcome):
                            if event["event"] == "done":
                                async with transaction() as conn:
                                    state["message_id"] = (await conn.execute(schema.messages.insert().values(
                                        conversation_id=conversation_id, role="assistant", content=outcome.answer,
                                        citations=outcome.citations, filters=outcome.filters, model=outcome.model,
                                        trace_id=t.id, status=outcome.status, groundedness=outcome.groundedness,
                                    ).returning(M.id))).scalar_one()
                                event["data"]["message_id"] = str(state["message_id"])
                                event["data"]["conversation_id"] = str(conversation_id)
                                t.attrs.update({"status": outcome.status, "model": outcome.model})
                            queue.put_nowait(("event", event))
            except Exception as exc:  # noqa: BLE001 - handed to the stream below, which raises it as before
                queue.put_nowait(("failed", exc))
            finally:
                queue.put_nowait(("end", None))

        task = asyncio.create_task(answer())
        try:
            while True:
                kind, item = await queue.get()
                if kind == "end":
                    break
                if kind == "failed":
                    raise item
                yield {"event": "trace", "data": item} if kind == "trace" else item
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if state["message_id"] is None and outcome.status in {"error", "refused"}:
                async with transaction() as conn:
                    await conn.execute(schema.messages.insert().values(
                        conversation_id=conversation_id, role="assistant", content=outcome.answer or "",
                        status=outcome.status, model=outcome.model, trace_id=state["trace_id"]))

    return sse_response(events())


@router.get("/conversations")
async def list_conversations(user: UserDep, limit: int = 50, offset: int = 0, q: str = "") -> dict:
    query = select(C.id, C.title, C.updated_at, C.created_at).where(C.user_id == user.id, C.archived.is_(False))
    if q:
        query = query.where(C.title.ilike(f"%{q[:100]}%"))
    async with transaction() as conn:
        rows = (await conn.execute(query.order_by(C.updated_at.desc()).limit(min(limit, 200)).offset(offset))).all()
    return {"conversations": [{"id": str(r.id), "title": r.title, "updated_at": r.updated_at.isoformat(),
                               "created_at": r.created_at.isoformat()} for r in rows]}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, user: UserDep) -> dict:
    async with transaction() as conn:
        conv = (await conn.execute(select(schema.conversations).where(C.id == conversation_id,
                                                                      C.user_id == user.id))).first()
        if conv is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
        rows = (await conn.execute(select(schema.messages).where(M.conversation_id == conversation_id)
                                   .order_by(M.created_at))).all()
    return {"id": str(conv.id), "title": conv.title, "filters": conv.filters, "messages": [{
        "id": str(r.id), "role": r.role, "content": r.content, "citations": r.citations, "filters": r.filters,
        "model": r.model, "status": r.status, "groundedness": r.groundedness, "trace_id": r.trace_id,
        "created_at": r.created_at.isoformat(), "feedback_score": r.feedback_score} for r in rows]}


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(conversation_id: UUID, body: ConversationPatch, user: UserDep) -> dict:
    values = body.model_dump(exclude_none=True)
    if not values:
        return {"ok": True}
    async with transaction() as conn:
        result = await conn.execute(update(schema.conversations).where(C.id == conversation_id, C.user_id == user.id)
                                    .values(**values).returning(C.id))
        if result.first() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return {"ok": True}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: UUID, user: UserDep) -> dict:
    async with transaction() as conn:
        result = await conn.execute(schema.conversations.delete().where(C.id == conversation_id,
                                                                        C.user_id == user.id).returning(C.id))
        if result.first() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return {"ok": True}


class FeedbackBody(BaseModel):
    score: int = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=2000)


@router.post("/messages/{message_id}/feedback")
async def feedback(message_id: UUID, body: FeedbackBody, user: UserDep) -> dict:
    async with transaction() as conn:
        result = await conn.execute(
            update(schema.messages)
            .where(M.id == message_id, M.conversation_id.in_(select(C.id).where(C.user_id == user.id)))
            .values(feedback_score=body.score, feedback_comment=body.comment or None,
                    feedback_at=datetime.now(UTC)).returning(M.id))
        if result.first() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    return {"ok": True}
