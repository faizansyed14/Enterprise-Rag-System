"""Checks run by `rag check`, at API start-up (logged) and on the Models/Monitor pages.

Each role: provider exists, its keys are set, and (for local models) the model server has the model. Plus database,
migrations, Qdrant, object store and the live index matching .env.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text

from rag.config.roles import ROLES
from rag.plugins.registry import registry

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    status: str  # ok | warn | fail
    detail: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


async def run_checks(services: Any) -> list[CheckResult]:
    s = services.settings
    results: list[CheckResult] = []

    await services.hub.refresh_overrides()  # what the Models page switched, not only what .env says
    for role in ROLES:
        spec = services.hub.spec(role)
        if spec.is_off:
            results.append(CheckResult(f"role {role}", "ok", "switched off"))
            continue
        info = registry.info(spec.capability, spec.provider)
        if info is None:
            results.append(CheckResult(f"role {role}", "fail", f"no {spec.capability.value} provider "
                                                               f"{spec.provider!r}"))
            continue
        missing = registry.missing_requirements(spec, s)
        if missing:
            level = "fail" if role in {"LLM_ANSWER", "EMBEDDER", "PARSER"} else "warn"
            results.append(CheckResult(f"role {role}", level, f"{spec.label} needs {', '.join(missing)}"))
        else:
            results.append(CheckResult(f"role {role}", "ok", spec.label))

    local_needed = sorted({spec.model for spec in services.hub.specs().values()
                           if spec.provider == "local" and spec.capability.value in {"embedder", "reranker",
                                                                                     "groundedness"}})
    if local_needed:
        try:
            from rag.providers.local_models import LocalModelClient

            client = LocalModelClient(s)
            try:
                data = await client.get("/models")
            finally:
                await client.aclose()
            have = {m["name"] for m in data.get("models", []) if m.get("downloaded")}
            missing_models = [m for m in local_needed if m not in have]
            if missing_models:
                results.append(CheckResult("model server", "fail",
                                           f"missing models: {', '.join(missing_models)} (run: make models)"))
            else:
                results.append(CheckResult("model server", "ok", f"{s.local_models_url} has {', '.join(local_needed)}"))
        except Exception as exc:  # noqa: BLE001 - reported as a failed check
            results.append(CheckResult("model server", "fail", f"{s.local_models_url} unreachable "
                                                               f"({type(exc).__name__})"))

    try:
        from rag.db.engine import transaction

        async with transaction() as conn:
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
        results.append(CheckResult("database", "ok", f"connected, migration {version}"))
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("database", "fail", f"{type(exc).__name__}: {str(exc)[:200]}"))

    try:
        collections = await services.vectors.collections()
        target = await services.vectors.alias_target()
        results.append(CheckResult("qdrant", "ok", f"{len(collections)} collections; search uses {target or '-'}"))
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("qdrant", "fail", f"{type(exc).__name__}: {str(exc)[:200]}"))

    try:
        await services.objects.put("healthcheck/probe", b"ok")
        results.append(CheckResult("object store", "ok", s.object_store_url))
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("object store", "fail", f"{type(exc).__name__}: {str(exc)[:200]}"))

    try:
        from rag.ingestion.index_builds import status

        index = await status(services)
        if index["rebuild_needed"]:
            results.append(CheckResult("index", "warn", "EMBEDDER/analyzer/chunker changed in .env; search still "
                                                        f"uses {index['live']['collection']}. Run: rag index rebuild"))
        elif index["live"]:
            results.append(CheckResult("index", "ok", f"{index['live']['collection']} "
                                                      f"({index['live_versions']} live documents)"))
        else:
            results.append(CheckResult("index", "warn", "no live index yet (created on first ingestion)"))
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("index", "warn", f"could not read index status ({type(exc).__name__})"))

    if s.app_env == "prod":
        if not s.secure_cookies:
            results.append(CheckResult("cookies", "fail", "COOKIE_SECURE must be true in production"))
        if s.openrouter_api_key and not s.openrouter_zdr:
            results.append(CheckResult("privacy", "warn", "OPENROUTER_ZDR=false: prompts may reach providers that "
                                                          "retain data"))
    from rag.cache import get_cache

    cache = get_cache(s)
    if not cache.enabled:
        results.append(CheckResult("redis", "ok", "not configured: caches stay per process and rate limits stay in Postgres"))
    else:
        info = await cache.stats()
        if info.get("available"):
            results.append(CheckResult("redis", "ok", f"connected, {info['keys']} keys, {info['memory']} used"))
        else:
            results.append(CheckResult("redis", "warn", "configured but not reachable: the in-process fallbacks are used"))
    return results
