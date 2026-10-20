"""Administration: the Retrieval Lab, users and groups, evaluation runs."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from rag.api.deps import AdminDep, PrincipalsDep, ServicesDep, client_ip
from rag.db import schema
from rag.db.engine import transaction
from rag.jobs import queue
from rag.jobs.queue import NewJob
from rag.retrieval.query_router import QueryRouter
from rag.retrieval.retriever import Retriever
from rag.retrieval.types import SearchFilters
from rag.security.audit import audit
from rag.security.auth import create_user, hash_password
from rag.security.principals import replace_principals

router = APIRouter(tags=["admin"])
U = schema.users.c


# ---- Retrieval Lab ------------------------------------------------------------------------------------------------------


class DebugBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    filters: dict[str, Any] = Field(default_factory=dict)
    rerank: bool = True
    use_router_model: bool = False


@router.post("/retrieval/debug")
async def retrieval_debug(body: DebugBody, _: AdminDep, principals: PrincipalsDep, services: ServicesDep) -> dict:
    """Every stage of a search for one question: plan, each leg's hits, fusion, rerank, final passages, timings.
    Runs with the admin's own access."""
    router_model = services.hub.try_get("LLM_ROUTER") if body.use_router_model else None
    router_ = QueryRouter(known_projects=await services.known_projects(), router_model=router_model,
                          timeout_s=services.settings.llm_router_timeout_s,
                          reranker_languages=services.settings.reranker_language_set)
    plan = await router_.plan(body.question, SearchFilters.from_dict(body.filters))
    result = await Retriever(services).search(plan, principals, rerank=body.rerank, debug=True)
    return {
        "plan": {"query": plan.query, "language": plan.language, "query_type": plan.query_type,
                 "identifiers": plan.identifiers, "english": plan.english, "hard": plan.hard,
                 "filters": plan.filters.as_dict(), "boosts": plan.boosts, "reasons": plan.reasons,
                 "router": plan.router},
        "weights": services.settings.fusion_weight_table.get(plan.query_type),
        "passages": [p.as_dict() for p in result.passages],
        "documents": [d.__dict__ for d in result.documents],
        "timings_ms": {k: round(v, 1) for k, v in result.timings_ms.items()},
        "debug": result.debug,
        "models": {"embedder": (await services.live_build()).embedder,
                   "reranker": services.hub.spec("RERANKER").label},
    }


# ---- users -------------------------------------------------------------------------------------------------------------


class UserBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(default="", max_length=200)
    password: str = Field(min_length=12, max_length=512)
    is_admin: bool = False
    groups: list[str] = Field(default_factory=list)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    is_admin: bool | None = None
    is_active: bool | None = None
    groups: list[str] | None = None
    password: str | None = Field(default=None, min_length=12, max_length=512)


def _groups(names: list[str]) -> list[str]:
    out = []
    for name in names:
        name = name.strip().lower()  # noqa: PLW2901
        if not name:
            continue
        out.append(name if name.startswith(("lgroup:", "group:")) else f"lgroup:{name}")
    return sorted(set(out))


@router.get("/users")
async def list_users(_: AdminDep, q: str = "", limit: int = 100, offset: int = 0) -> dict:
    query = select(schema.users)
    if q:
        query = query.where(U.email.ilike(f"%{q[:100]}%") | U.display_name.ilike(f"%{q[:100]}%"))
    async with transaction() as conn:
        rows = (await conn.execute(query.order_by(U.created_at).limit(min(limit, 500)).offset(offset))).all()
        groups = (await conn.execute(select(schema.user_principals.c.user_id, schema.user_principals.c.principal,
                                            schema.user_principals.c.source))).all()
        total = (await conn.execute(select(func.count()).select_from(schema.users))).scalar_one()
    by_user: dict[str, list[dict[str, str]]] = {}
    for g in groups:
        by_user.setdefault(str(g.user_id), []).append({"principal": g.principal, "source": g.source})
    return {"total": total, "users": [{
        "id": str(r.id), "email": r.email, "display_name": r.display_name, "auth_provider": r.auth_provider,
        "is_admin": r.is_admin, "is_active": r.is_active, "created_at": r.created_at.isoformat(),
        "last_login_at": r.last_login_at.isoformat() if r.last_login_at else None,
        "groups": by_user.get(str(r.id), [])} for r in rows]}


@router.post("/users")
async def add_user(body: UserBody, request: Request, admin: AdminDep) -> dict:
    async with transaction() as conn:
        exists = (await conn.execute(select(U.id).where(func.lower(U.email) == body.email.lower()))).first()
        if exists:
            raise HTTPException(status.HTTP_409_CONFLICT, "A user with this e-mail exists")
        user_id = await create_user(conn, email=body.email, password=body.password, display_name=body.display_name,
                                    is_admin=body.is_admin)
        await replace_principals(conn, user_id, _groups(body.groups), "local")
    await audit("user_create", user_id=admin.id, target=body.email, ip=client_ip(request),
                details={"is_admin": body.is_admin, "groups": _groups(body.groups)})
    return {"id": str(user_id)}


@router.patch("/users/{user_id}")
async def patch_user(user_id: UUID, body: UserPatch, request: Request, admin: AdminDep) -> dict:
    values: dict[str, Any] = {k: v for k, v in body.model_dump(exclude_none=True).items()
                              if k in {"display_name", "is_admin", "is_active"}}
    if user_id == admin.id and (values.get("is_admin") is False or values.get("is_active") is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot remove your own admin rights or disable yourself")
    async with transaction() as conn:
        if body.password:
            values["password_hash"] = hash_password(body.password)
        if values:
            result = await conn.execute(update(schema.users).where(U.id == user_id).values(**values).returning(U.id))
            if result.first() is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
        if body.groups is not None:
            await replace_principals(conn, user_id, _groups(body.groups), "local")
        if body.is_active is False or body.password:
            await conn.execute(schema.sessions.delete().where(schema.sessions.c.user_id == user_id))
    await audit("user_update", user_id=admin.id, target=str(user_id), ip=client_ip(request),
                details={k: v for k, v in values.items() if k != "password_hash"} | (
                    {"groups": _groups(body.groups)} if body.groups is not None else {}))
    return {"ok": True}


# ---- evaluation ----------------------------------------------------------------------------------------------------------


class EvalBody(BaseModel):
    name: str = Field(default="", max_length=120)
    golden_set: str = Field(default="demo", max_length=200)
    answer: bool = True


@router.get("/eval/sets")
async def list_sets(_: AdminDep) -> dict:
    from rag.evaluation.golden import available_sets

    return {"sets": available_sets()}


@router.get("/eval/runs")
async def list_runs(_: AdminDep, limit: int = 50) -> dict:
    async with transaction() as conn:
        rows = (await conn.execute(select(schema.eval_runs).order_by(schema.eval_runs.c.created_at.desc())
                                   .limit(min(limit, 200)))).all()
    return {"runs": [{"id": str(r.id), "name": r.name, "golden_set": r.golden_set, "status": r.status,
                      "config": r.config, "metrics": r.metrics, "error": r.error,
                      "created_at": r.created_at.isoformat(),
                      "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in rows]}


@router.post("/eval/runs")
async def start_run(body: EvalBody, request: Request, admin: AdminDep, services: ServicesDep) -> dict:
    from rag.evaluation.golden import available_sets

    if body.golden_set not in available_sets():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown golden set; available: {', '.join(available_sets())}")
    hub = services.hub
    config = {"embedder": (await services.live_build()).embedder, "reranker": hub.spec("RERANKER").label,
              "llm_answer": hub.spec("LLM_ANSWER").label, "fusion_weights": services.settings.fusion_weight_table,
              "answer": body.answer}
    async with transaction() as conn:
        run_id = (await conn.execute(schema.eval_runs.insert().values(
            name=body.name or f"{body.golden_set} run", golden_set=body.golden_set, config=config,
            created_by=admin.id).returning(schema.eval_runs.c.id))).scalar_one()
        await queue.enqueue(conn, NewJob("eval_run", {"run_id": str(run_id), "principal_user": str(admin.id)},
                                         lane="interactive"))
    await audit("eval_start", user_id=admin.id, target=str(run_id), ip=client_ip(request))
    return {"id": str(run_id)}


@router.get("/eval/runs/{run_id}")
async def get_run(run_id: UUID, _: AdminDep) -> dict:
    async with transaction() as conn:
        run = (await conn.execute(select(schema.eval_runs).where(schema.eval_runs.c.id == run_id))).first()
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
        items = (await conn.execute(select(schema.eval_items).where(schema.eval_items.c.run_id == run_id)
                                    .order_by(schema.eval_items.c.item_id))).all()
    return {"id": str(run.id), "name": run.name, "golden_set": run.golden_set, "status": run.status,
            "config": run.config, "metrics": run.metrics, "error": run.error,
            "items": [{"item_id": i.item_id, "question": i.question, "expected": i.expected,
                       "retrieved": i.retrieved, "answer": i.answer, "metrics": i.metrics,
                       "latency_ms": i.latency_ms} for i in items]}
