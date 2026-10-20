"""Shared test fixtures.

Unit tests need nothing. Integration and end-to-end tests use a real Postgres and a real Qdrant:

    RAG_TEST_DATABASE_URL  (default postgresql://rag:rag@localhost:5432/rag_test)
    RAG_TEST_QDRANT_URL    (default http://localhost:6333)

`scripts/test.sh` starts both with Docker when they are not already running. If they are unreachable, those tests
are skipped with a message rather than failing on connection errors.
"""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest

from rag.config.settings import Settings, set_settings

TEST_DB = os.environ.get("RAG_TEST_DATABASE_URL", "postgresql://rag:rag@localhost:5432/rag_test")
TEST_QDRANT = os.environ.get("RAG_TEST_QDRANT_URL", "http://localhost:6333")
TESTS_DIR = Path(__file__).parent
BACKEND_DIR = TESTS_DIR.parent / "backend"
SERVICE_DIRS = ("integration", "e2e", "eval")


def _reachable(url: str, default_port: int) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or default_port), timeout=1.5):
            return True
    except OSError:
        return False


SERVICES_UP = _reachable(TEST_DB, 5432) and _reachable(TEST_QDRANT, 6333)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(reason=f"needs Postgres ({TEST_DB}) and Qdrant ({TEST_QDRANT}); run scripts/test.sh")
    for item in items:
        parts = set(Path(str(item.path)).parts)
        if parts & set(SERVICE_DIRS):
            item.add_marker(pytest.mark.integration)
            if not SERVICES_UP:
                item.add_marker(skip)


def make_test_settings(tmp: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": TEST_DB,
        "qdrant_url": TEST_QDRANT,
        "qdrant_collection_prefix": "test_chunks",
        "qdrant_alias": "test_chunks_live",
        "qdrant_on_disk": False,
        "qdrant_quantization": "none",
        "object_store_url": f"file://{tmp}/originals",
        "cache_dir": str(tmp / "cache"),
        "llm_answer": "extractive/default",
        "llm_answer_hard": "extractive/default",
        "llm_answer_fallback": "extractive/default",
        "llm_router": "none",
        "llm_enrich": "none",
        "vision": "none",
        "embedder": "hashing/hash-384",
        "reranker": "lexical/overlap",
        "groundedness": "builtin/lexical",
        "parser": "local/pdfium",
        "ocr": "local/rapidocr",
        "local_models_url": "http://127.0.0.1:9",
        "allowed_origins": "http://testserver,http://localhost:3000",
        "rate_limit_chat_per_min": 1000,
        "login_attempts_per_15min": 1000,
        "bootstrap_admin_email": "",
        "openrouter_api_key": None,
        "openai_api_key": None,
        "anthropic_api_key": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("ragdata")


@pytest.fixture(scope="session")
def settings(data_dir: Path) -> Settings:
    s = make_test_settings(data_dir)
    set_settings(s)
    yield s
    set_settings(None)


@pytest.fixture(scope="session")
def migrated_db(settings: Settings) -> str:
    """Fresh schema for the whole session: drop everything, then run the real Alembic migrations."""
    if not SERVICES_UP:
        pytest.skip("Postgres not reachable")
    import psycopg
    from alembic import command
    from alembic.config import Config

    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    return TEST_DB


@pytest.fixture
async def db(migrated_db: str):  # noqa: ANN201
    """Empty tables before each test that asks for the database."""
    from sqlalchemy import text

    from rag.db.engine import transaction
    from rag.db.schema import metadata

    names = ", ".join(f'"{t.name}"' for t in metadata.sorted_tables)
    async with transaction() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def originals_dir(settings: Settings) -> Path:
    path = Path(settings.object_store_url.removeprefix("file://"))
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def corpus_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The demo construction library (written once per test session)."""
    from rag.evaluation.demo_corpus import build_demo_corpus

    root = tmp_path_factory.mktemp("corpus")
    build_demo_corpus(root)
    return root
