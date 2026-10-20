"""Models page: which model each role uses right now (and where that came from), what each provider needs, live
tests, switching chat roles from the UI (admins), and index builds."""

from __future__ import annotations

import base64
import io
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert

from rag.api.deps import AdminDep, ServicesDep, UserDep, client_ip
from rag.config.roles import ROLES, UI_SWITCHABLE_ROLES, RoleSpecError, parse_role
from rag.db import schema
from rag.db.engine import transaction
from rag.ingestion import index_builds
from rag.plugins.capabilities import ChatMessage
from rag.plugins.registry import registry
from rag.security.audit import audit

router = APIRouter(tags=["models"])


@router.get("/models")
async def models(_: UserDep, services: ServicesDep) -> dict:
    await services.hub.refresh_overrides(force=True)
    s = services.settings
    local: dict[str, Any] = {"url": s.local_models_url, "reachable": False, "models": []}
    try:
        from rag.providers.local_models import LocalModelClient

        client = LocalModelClient(s)
        try:
            data = await client.get("/models")
            local.update(reachable=True, models=data.get("models", []))
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 - shown as "not reachable"
        local["error"] = type(exc).__name__
    providers = [{"capability": p.capability.value, "provider": p.provider, "description": p.description,
                  "local": p.local, "requires": [r.upper() for r in p.requires],
                  "configured": not [r for r in p.requires if not s.secret(r)]} for p in registry.providers()]
    return {"roles": services.hub.describe(), "choices": s.model_choice_lines, "providers": providers,
            "local_server": local, "allow_switch": s.allow_ui_model_switch, "show_badge": s.show_model_badge,
            "zdr": s.openrouter_zdr}


class TestBody(BaseModel):
    role: str


def _tiny_png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (360, 80), "white")
    ImageDraw.Draw(image).text((10, 30), "TEST 123", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@router.post("/models/test")
async def test_role(body: TestBody, _: AdminDep, services: ServicesDep) -> dict:
    role = body.role.upper()
    if role not in ROLES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown role")
    return await run_role_test(services, role)


async def run_role_test(services: Any, role: str) -> dict[str, Any]:
    """One real call with the model of a role (used by the Models page and `rag models test`)."""
    spec = services.hub.spec(role)
    if spec.is_off:
        return {"ok": True, "detail": "This role is switched off", "ms": 0}
    start = time.perf_counter()
    try:
        plugin = services.hub.get(role)
        cap = spec.capability.value
        if cap in {"chat", "vision"}:
            messages = [ChatMessage("user", "Reply with the single word: ready")]
            if cap == "vision":
                messages = [ChatMessage("user", [{"type": "text", "text": "What text is in this image?"},
                                                 {"type": "image", "media_type": "image/png",
                                                  "data": base64.b64encode(_tiny_png()).decode()}])]
            result = await plugin.complete(messages, max_tokens=20, temperature=0.0)
            detail = result.content.strip()[:120] or "(empty reply)"
        elif cap == "embedder":
            vector = await plugin.embed_query("concrete grade for columns")
            detail = f"{len(vector)} dimensions"
        elif cap == "reranker":
            scores = await plugin.rerank("concrete curing", ["Cure concrete for 7 days.", "Paint the doors."])
            detail = f"scores {', '.join(f'{x:.2f}' for x in scores)}"
        elif cap == "groundedness":
            scores = await plugin.score([("Concrete grade is C40.", "The grade is C40.")])
            detail = f"support {scores[0]:.2f}"
        elif cap == "ocr":
            result = await plugin.recognize(_tiny_png())
            detail = f"read: {result.text[:60]!r}"
        else:
            detail = "parser loaded"
    except Exception as exc:  # noqa: BLE001 - the admin sees the failure
        return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
                "ms": round((time.perf_counter() - start) * 1000)}
    return {"ok": True, "detail": detail, "ms": round((time.perf_counter() - start) * 1000), "model": spec.label}


@router.get("/models/options/{role}")
async def role_options(role: str, _: AdminDep, services: ServicesDep) -> dict:
    """Everything the switch dialog needs for one role, so nothing is typed by hand: the providers that can do the job (with
    whether their key is set) and, for each, the models it offers. OpenRouter's list comes from its public catalogue with
    prices; each model says whether it reads images, and for the vision role only those can be chosen."""
    from rag.providers import openrouter_catalog

    role = role.upper()
    if role not in UI_SWITCHABLE_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{role} is set in .env")
    s = services.settings
    capability = ROLES[role]
    answers = role.startswith("LLM_ANSWER")
    catalogue = openrouter_catalog.curate(await openrouter_catalog.models(), s.openrouter_models)
    images_required = capability.value == "vision"  # the models are all listed, tagged, but only image readers can be chosen
    providers: list[dict[str, Any]] = []
    for info in registry.providers():
        if info.capability != capability:
            continue
        if info.provider == "extractive" and not answers:
            continue  # sentences from the passages can answer a question, but not route, enrich or read a page
        requires = [r.upper() for r in info.requires]
        entry: dict[str, Any] = {"provider": info.provider, "description": info.description, "local": info.local,
                                 "requires": requires, "configured": not [r for r in info.requires if not s.secret(r)],
                                 "models": [], "custom": False}
        if info.provider == "openrouter":
            entry["models"] = [{"id": m["id"], "name": m["name"], "free": m["free"], "vision": m["vision"],
                                "prompt_per_m": m["prompt_per_m"], "completion_per_m": m["completion_per_m"]} for m in catalogue]
        elif info.provider == "extractive":
            entry["models"] = [{"id": "default", "name": "Sentences from the passages (no model)", "free": True}]
        elif capability.value == "ocr":
            names = {"local": ("rapidocr", "RapidOCR on this server: free, English and Chinese, loose lines"),
                     "vision": ("default", "The vision model reads each page: tables, handwriting and pictures too. One model call per page")}
            model_id, name = names.get(info.provider, ("default", info.description))
            entry["models"] = [{"id": model_id, "name": name, "free": info.local}]
        else:
            entry["custom"] = True  # a provider without a public catalogue: the model id is typed
        providers.append(entry)
    providers.sort(key=lambda p: (p["provider"] != "openrouter", not p["configured"], p["provider"]))
    return {"role": role, "current": services.hub.spec(role).label, "providers": providers,
            "catalogue_available": bool(catalogue), "zdr": s.openrouter_zdr, "images_required": images_required}


class SwitchBody(BaseModel):
    spec: str = Field(min_length=3, max_length=300)
    reason: str = Field(default="", max_length=300)


@router.put("/models/{role}")
async def switch_role(role: str, body: SwitchBody, request: Request, user: AdminDep, services: ServicesDep) -> dict:
    role = role.upper()
    s = services.settings
    if not s.allow_ui_model_switch:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Model switching is disabled (ALLOW_UI_MODEL_SWITCH=false)")
    if role not in UI_SWITCHABLE_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{role} changes the index or ingestion; change it in .env (and rebuild if needed)")
    try:
        spec = parse_role(role, body.spec)
    except RoleSpecError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if role == "OCR" and spec.provider == "vision" and services.hub.spec("VISION").is_off:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Choose a model for 'Drawing and image reading' first: reading pages with vision uses that model")
    if not spec.is_off:
        info = registry.info(spec.capability, spec.provider)
        if info is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No {spec.capability.value} provider named {spec.provider}")
        missing = registry.missing_requirements(spec, s)
        if missing:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{spec.label} needs {', '.join(missing)} in .env")
    previous = services.hub.spec(role).label
    async with transaction() as conn:
        await conn.execute(insert(schema.role_overrides).values(role=role, spec=spec.label, updated_by=user.id)
                           .on_conflict_do_update(index_elements=[schema.role_overrides.c.role],
                                                  set_={"spec": spec.label, "updated_by": user.id}))
        await conn.execute(schema.model_switches.insert().values(role=role, from_spec=previous, to_spec=spec.label,
                                                                 switched_by=user.id, reason=body.reason))
    await services.hub.refresh_overrides(force=True)
    await audit("model_switch", user_id=user.id, target=role, ip=client_ip(request),
                details={"from": previous, "to": spec.label})
    return {"role": role, "spec": spec.label, "previous": previous}


@router.delete("/models/{role}")
async def reset_role(role: str, request: Request, user: AdminDep, services: ServicesDep) -> dict:
    role = role.upper()
    previous = services.hub.spec(role).label
    async with transaction() as conn:
        await conn.execute(schema.role_overrides.delete().where(schema.role_overrides.c.role == role))
        await conn.execute(schema.model_switches.insert().values(
            role=role, from_spec=previous, to_spec=services.hub.env_spec(role).label, switched_by=user.id,
            reason="reset to .env"))
    await services.hub.refresh_overrides(force=True)
    await audit("model_reset", user_id=user.id, target=role, ip=client_ip(request))
    return {"role": role, "spec": services.hub.spec(role).label}


@router.get("/models/switches")
async def switch_history(_: AdminDep, limit: int = 50) -> dict:
    from sqlalchemy import select

    async with transaction() as conn:
        rows = (await conn.execute(select(schema.model_switches, schema.users.c.email)
                                   .outerjoin(schema.users, schema.users.c.id == schema.model_switches.c.switched_by)
                                   .order_by(schema.model_switches.c.switched_at.desc()).limit(min(limit, 200)))).all()
    return {"switches": [{"role": r.role, "from": r.from_spec, "to": r.to_spec, "by": r.email, "reason": r.reason,
                          "at": r.switched_at.isoformat()} for r in rows]}


# ---- index builds -------------------------------------------------------------------------------------------------------


@router.get("/index")
async def index_status(_: AdminDep, services: ServicesDep) -> dict:
    return await index_builds.status(services)


@router.post("/index/rebuild")
async def index_rebuild(request: Request, user: AdminDep, services: ServicesDep) -> dict:
    result = await index_builds.start_rebuild(services)
    await audit("index_rebuild", user_id=user.id, ip=client_ip(request), details=result)
    return result


class ActivateBody(BaseModel):
    build_id: str


@router.post("/index/activate")
async def index_activate(body: ActivateBody, request: Request, user: AdminDep, services: ServicesDep) -> dict:
    result = await index_builds.activate(services, body.build_id)
    await audit("index_activate", user_id=user.id, ip=client_ip(request), details=result)
    return result


@router.post("/index/rollback")
async def index_rollback(request: Request, user: AdminDep, services: ServicesDep) -> dict:
    result = await index_builds.rollback(services)
    await audit("index_rollback", user_id=user.id, ip=client_ip(request), details=result)
    return result
