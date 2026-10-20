"""The HTTP API end to end: sign-in, sessions, CSRF, roles, chat over SSE, documents, page images and highlights,
uploads with their own access, model switching, monitoring, evaluation, health and security headers."""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from rag.api.app import create_app
from rag.db.engine import transaction
from rag.db.schema import metadata
from rag.evaluation.demo_corpus import _pdf
from rag.jobs import queue
from rag.jobs.handlers import load_handlers
from rag.jobs.queue import NewJob
from rag.security.auth import create_user
from rag.security.principals import replace_principals
from rag.services import Services

from .conftest import add_source, reset_qdrant, run_jobs

PASSWORD = "Correct-Horse-Battery-9"
ORIGIN = "http://localhost:3000"


@pytest.fixture(scope="module")
async def stack(migrated_db, settings, corpus_root, tmp_path_factory):  # noqa: ANN001, ANN201
    load_handlers()
    names = ", ".join(f'"{t.name}"' for t in metadata.sorted_tables)
    async with transaction() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    shutil.rmtree(Path(settings.object_store_url.removeprefix("file://")), ignore_errors=True)
    corpus = tmp_path_factory.mktemp("api") / "corpus"
    shutil.copytree(corpus_root, corpus)
    services = Services.create(settings)
    await reset_qdrant(services)
    async with transaction() as conn:
        await create_user(conn, email="admin@test.local", password=PASSWORD, is_admin=True)
        await create_user(conn, email="engineer@test.local", password=PASSWORD)
        boss = await create_user(conn, email="boss@test.local", password=PASSWORD)
        await replace_principals(conn, boss, ["lgroup:commercial"], "local")
    for name, folder, acl in (("Library", corpus / "public", ["everyone"]),
                              ("Commercial", corpus / "restricted", ["lgroup:commercial"])):
        source = await add_source(name, folder, acl)
        async with transaction() as conn:
            await queue.enqueue(conn, NewJob("sync_source", {"source_id": source}, lane="interactive"))
    await run_jobs(services)
    app = create_app(settings, services)
    app.state.services = services
    yield app, services
    await services.aclose()


@asynccontextmanager
async def client_for(app: Any, email: str | None = None) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                 headers={"Origin": ORIGIN}) as client:
        if email:
            response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
            assert response.status_code == 200, response.text
            client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield client


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = [ln for ln in block.split("\n") if not ln.startswith(":")]
        name = next((ln[7:] for ln in lines if ln.startswith("event: ")), None)
        data = next((ln[6:] for ln in lines if ln.startswith("data: ")), None)
        if name and data:
            out.append((name, json.loads(data)))
    return out


async def test_sign_in_sessions_and_csrf(stack) -> None:  # noqa: ANN001
    app, _ = stack
    async with client_for(app) as anon:
        assert (await anon.get("/api/v1/auth/me")).status_code == 401
        bad = await anon.post("/api/v1/auth/login", json={"email": "engineer@test.local", "password": "wrong-one!"})
        assert bad.status_code == 401
        ok = await anon.post("/api/v1/auth/login", json={"email": "ENGINEER@test.local", "password": PASSWORD})
        assert ok.status_code == 200
        cookie = ok.headers.get_list("set-cookie")
        assert any("rag_session=" in c and "HttpOnly" in c and "SameSite=lax" in c for c in cookie)
        me = await anon.get("/api/v1/auth/me")
        assert me.json()["user"]["email"] == "engineer@test.local"
        # Unsafe request without the CSRF token is refused; with it, accepted.
        no_csrf = await anon.post("/api/v1/messages/00000000-0000-0000-0000-000000000000/feedback",
                                  json={"score": 1})
        assert no_csrf.status_code == 403
        anon.headers["X-CSRF-Token"] = ok.json()["csrf_token"]
        with_csrf = await anon.post("/api/v1/messages/00000000-0000-0000-0000-000000000000/feedback",
                                    json={"score": 1})
        assert with_csrf.status_code == 404
        evil = await anon.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403
        assert (await anon.post("/api/v1/auth/logout")).status_code == 200
        assert (await anon.get("/api/v1/auth/me")).status_code == 401


async def test_lockout_after_repeated_failures(stack) -> None:  # noqa: ANN001
    app, _ = stack
    async with transaction() as conn:
        await create_user(conn, email="victim@test.local", password=PASSWORD)
    async with client_for(app) as anon:
        for _ in range(8):
            await anon.post("/api/v1/auth/login", json={"email": "victim@test.local", "password": "not-it-at-all"})
        locked = await anon.post("/api/v1/auth/login", json={"email": "victim@test.local", "password": PASSWORD})
        assert locked.status_code == 401


async def test_only_failed_sign_ins_count_against_an_address(stack) -> None:  # noqa: ANN001
    """Many people behind one office address can sign in; repeated failures from it are slowed down."""
    app, services = stack
    original = services.settings.login_attempts_per_15min
    services.settings.login_attempts_per_15min = 3
    async with transaction() as conn:
        await conn.execute(text("DELETE FROM rate_limits WHERE key LIKE 'login:%'"))
    try:
        async with client_for(app) as anon:
            for _ in range(5):
                ok = await anon.post("/api/v1/auth/login", json={"email": "engineer@test.local", "password": PASSWORD})
                assert ok.status_code == 200
            for _ in range(3):
                bad = await anon.post("/api/v1/auth/login", json={"email": "nobody@test.local", "password": "guess-1234"})
                assert bad.status_code == 401
            blocked = await anon.post("/api/v1/auth/login", json={"email": "engineer@test.local", "password": PASSWORD})
            assert blocked.status_code == 429
            assert int(blocked.headers["Retry-After"]) > 0
    finally:
        services.settings.login_attempts_per_15min = original
        async with transaction() as conn:
            await conn.execute(text("DELETE FROM rate_limits WHERE key LIKE 'login:%'"))


async def test_admin_pages_need_the_admin_role(stack) -> None:  # noqa: ANN001
    app, _ = stack
    async with client_for(app, "engineer@test.local") as user, client_for(app, "admin@test.local") as admin:
        for path in ("/api/v1/sources", "/api/v1/monitor/summary", "/api/v1/users", "/api/v1/jobs", "/api/v1/index"):
            assert (await user.get(path)).status_code == 403, path
            assert (await admin.get(path)).status_code == 200, path


async def test_chat_streams_and_saves_the_conversation(stack) -> None:  # noqa: ANN001
    app, _ = stack
    async with client_for(app, "engineer@test.local") as client:
        response = await client.post("/api/v1/chat", json={"question": "What concrete cover is required for columns?"})
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        events = _events(response.text)
        names = [n for n, _ in events]
        assert names[0] == "conversation" and "delta" in names and "citation" in names and names[-1] == "done"
        done = events[-1][1]
        assert done["status"] == "ok" and "50 mm" in done["answer"]
        conversation_id = done["conversation_id"]
        listed = (await client.get("/api/v1/conversations")).json()["conversations"]
        assert any(c["id"] == conversation_id for c in listed)
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
        assert detail["messages"][1]["citations"][0]["verified"] is True
        fb = await client.post(f"/api/v1/messages/{done['message_id']}/feedback", json={"score": 1, "comment": "ok"})
        assert fb.status_code == 200
    async with client_for(app, "boss@test.local") as other:
        assert (await other.get(f"/api/v1/conversations/{conversation_id}")).status_code == 404


async def test_documents_follow_access_rights(stack) -> None:  # noqa: ANN001
    app, _ = stack
    async with client_for(app, "engineer@test.local") as engineer, client_for(app, "boss@test.local") as boss:
        visible = {d["doc_number"] for d in (await engineer.get("/api/v1/documents?limit=200")).json()["documents"]}
        assert "VO-15" not in visible and "RFI-023" in visible
        boss_docs = (await boss.get("/api/v1/documents?q=VO-15")).json()["documents"]
        assert boss_docs and boss_docs[0]["doc_number"] == "VO-15"
        vo_id = boss_docs[0]["id"]
        assert (await engineer.get(f"/api/v1/documents/{vo_id}")).status_code == 404
        assert (await engineer.get(f"/api/v1/documents/{vo_id}/download")).status_code == 404
        facets = (await engineer.get("/api/v1/documents/facets")).json()
        assert {"value": "Marina Tower", "count": facets["project"][0]["count"]} in facets["project"]


async def test_page_image_text_and_highlights(stack) -> None:  # noqa: ANN001
    app, _ = stack
    async with client_for(app, "engineer@test.local") as client:
        docs = (await client.get("/api/v1/documents?q=03 30 00")).json()["documents"]
        spec = next(d for d in docs if "Marina Tower" in d["project_ids"])
        detail = (await client.get(f"/api/v1/documents/{spec['id']}")).json()
        assert len(detail["versions"]) == 1 and "revision" not in detail["versions"][0]
        page = (await client.get(f"/api/v1/documents/{spec['id']}/pages/2")).json()
        assert "35 degrees" in page["text"] and page["has_image"]
        image = await client.get(f"/api/v1/documents/{spec['id']}/pages/2/image?scale=1")
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
        boxes = (await client.get(f"/api/v1/documents/{spec['id']}/pages/2/highlights",
                                  params={"q": "ambient temperature exceeds 35 degrees C"})).json()["boxes"]
        assert boxes and all(0 <= v <= 1 for box in boxes for v in box)
        download = await client.get(f"/api/v1/documents/{spec['id']}/download")
        assert download.status_code == 200 and download.content.startswith(b"%PDF")


async def test_uploads_are_private_to_the_uploader(stack, tmp_path) -> None:  # noqa: ANN001
    app, services = stack
    pdf = tmp_path / "Shop drawing note.pdf"
    _pdf(pdf, [["## SHOP DRAWING NOTE", "Glazing panels on the north facade use laminated glass type GL-7."]])
    async with client_for(app, "engineer@test.local") as client:
        bad = await client.post("/api/v1/uploads", files={"files": ("evil.exe", b"MZ\x90\x00binary", "text/plain")})
        assert bad.json()["files"][0]["ok"] is False
        response = await client.post("/api/v1/uploads", files={"files": (pdf.name, pdf.read_bytes(),
                                                                          "application/pdf")})
        item = response.json()["files"][0]
        assert item["ok"], item
        await run_jobs(services)
        status = (await client.get(f"/api/v1/uploads/{item['item_id']}")).json()
        assert status["stage"] == "live" and status["document_id"]
        mine = await client.post("/api/v1/chat", json={"question": "Which glass type is used for the north facade?"})
        assert "GL-7" in _events(mine.text)[-1][1]["answer"]
    async with client_for(app, "boss@test.local") as other:
        theirs = await other.post("/api/v1/chat", json={"question": "Which glass type is used for the north facade?"})
        assert "GL-7" not in _events(theirs.text)[-1][1]["answer"]


async def test_delete_document_removes_file_and_related_data(stack, tmp_path) -> None:  # noqa: ANN001
    app, services = stack
    pdf = tmp_path / "Temporary note.pdf"
    _pdf(pdf, [["## TEMP NOTE", "Delete-me marker phrase ZX-991."]])
    async with client_for(app, "engineer@test.local") as client:
        response = await client.post("/api/v1/uploads", files={"files": (pdf.name, pdf.read_bytes(),
                                                                          "application/pdf")})
        item = response.json()["files"][0]
        assert item["ok"], item
        await run_jobs(services)
        doc_id = (await client.get(f"/api/v1/uploads/{item['item_id']}")).json()["document_id"]
        assert doc_id
        assert (await client.get(f"/api/v1/documents/{doc_id}")).status_code == 200
        deleted = (await client.delete(f"/api/v1/documents/{doc_id}")).json()
        assert deleted["ok"] and deleted["files_removed"] >= 1
        assert (await client.get(f"/api/v1/documents/{doc_id}")).status_code == 404
        assert not await services.objects.exists(f"sha256/{item['sha256'][:2]}/{item['sha256'][2:4]}/{item['sha256']}")
    async with client_for(app, "boss@test.local") as other:
        assert (await other.delete(f"/api/v1/documents/{doc_id}")).status_code == 404


async def test_model_switching_rules(stack) -> None:  # noqa: ANN001
    app, _ = stack
    async with client_for(app, "admin@test.local") as admin:
        models = (await admin.get("/api/v1/models")).json()
        roles = {r["role"]: r for r in models["roles"]}
        assert roles["LLM_ANSWER"]["label"] == "extractive/default" and roles["EMBEDDER"]["local"]
        not_switchable = await admin.put("/api/v1/models/EMBEDDER", json={"spec": "hashing/hash-256"})
        assert not_switchable.status_code == 400
        missing_key = await admin.put("/api/v1/models/LLM_ANSWER",
                                      json={"spec": "openrouter/anthropic/claude-haiku-5.5"})
        assert missing_key.status_code == 400 and "OPENROUTER_API_KEY" in missing_key.json()["detail"]
        test = (await admin.post("/api/v1/models/test", json={"role": "EMBEDDER"})).json()
        assert test["ok"] and "384" in test["detail"]


async def test_monitor_jobs_retrieval_lab_and_eval(stack) -> None:  # noqa: ANN001
    app, services = stack
    from rag.monitoring.recorder import get_recorder

    await get_recorder().flush()
    async with client_for(app, "admin@test.local") as admin:
        summary = (await admin.get("/api/v1/monitor/summary?minutes=60")).json()
        assert summary["content"]["live_versions"] >= 13
        assert any(m["kind"] == "embedder" for m in summary["models"])
        jobs = (await admin.get("/api/v1/jobs?status_filter=done&limit=5")).json()
        assert jobs["jobs"] and "lanes" in jobs["stats"]
        lab = (await admin.post("/api/v1/retrieval/debug", json={"question": "RFI-023 response"})).json()
        assert lab["plan"]["query_type"] in {"identifier", "keyword", "question"}
        assert lab["debug"]["legs"]["exact"] and lab["passages"]
        run = (await admin.post("/api/v1/eval/runs", json={"golden_set": "demo", "answer": True})).json()
        await run_jobs(services)
        result = (await admin.get(f"/api/v1/eval/runs/{run['id']}")).json()
        assert result["status"] == "done" and result["metrics"]["questions"] == 16
        assert result["metrics"]["hit@5"] >= 0.8


async def test_health_metrics_and_security_headers(stack) -> None:  # noqa: ANN001
    app, _ = stack
    async with client_for(app) as anon:
        health = await anon.get("/api/v1/health")
        assert health.json() == {"status": "ok"}
        assert health.headers["x-content-type-options"] == "nosniff"
        assert health.headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in health.headers["content-security-policy"]
        assert (await anon.get("/api/v1/health/ready")).status_code == 200
        assert (await anon.get("/metrics")).status_code == 404  # disabled unless PROMETHEUS_METRICS=true
        invalid = await anon.post("/api/v1/auth/login", json={"email": "x"})
        assert invalid.status_code == 422 and invalid.json()["detail"] == "Invalid request"


async def test_prometheus_metrics_when_enabled(stack, monkeypatch) -> None:  # noqa: ANN001
    app, services = stack
    monkeypatch.setattr(services.settings, "prometheus_metrics", True)
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret")
    async with client_for(app) as anon:
        assert (await anon.get("/metrics")).status_code == 401
        scraped = await anon.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})
        assert scraped.status_code == 200
        body = scraped.text
        for name in ("rag_model_calls_24h", "rag_questions_5m", "rag_answer_p95_ms_5m", "rag_queue_jobs",
                     "rag_cost_usd_today", "rag_daily_budget_usd", "rag_live_documents"):
            assert f"# TYPE {name} gauge" in body, name
