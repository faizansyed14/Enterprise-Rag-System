from __future__ import annotations

import pytest
from pydantic import ValidationError

from rag.config.roles import Capability, RoleSpecError, parse_choices, parse_role
from rag.config.settings import Settings


def test_openrouter_line_keeps_slashes_in_model_id() -> None:
    spec = parse_role("LLM_ANSWER", "openrouter/anthropic/claude-haiku-5.5")
    assert spec.provider == "openrouter"
    assert spec.model == "anthropic/claude-haiku-5.5"
    assert spec.capability is Capability.CHAT
    assert spec.label == "openrouter/anthropic/claude-haiku-5.5"
    assert not spec.is_local


def test_direct_provider_and_local_lines() -> None:
    assert parse_role("LLM_ANSWER", "anthropic/claude-haiku-5-5").model == "claude-haiku-5-5"
    local = parse_role("EMBEDDER", "local/multilingual-e5-small")
    assert local.is_local and local.capability is Capability.EMBEDDER


def test_dimensions_and_options() -> None:
    spec = parse_role("EMBEDDER", "openai/text-embedding-3-large@1024")
    assert spec.model == "text-embedding-3-large"
    assert spec.dims == 1024
    assert spec.label.endswith("@1024")
    ocr = parse_role("OCR", "azure_di/prebuilt-read?pages_per_call=10&locale=ar")
    assert ocr.options == {"pages_per_call": "10", "locale": "ar"}


@pytest.mark.parametrize("line", ["none", "off", "NONE"])
def test_roles_can_be_switched_off(line: str) -> None:
    spec = parse_role("RERANKER", line)
    assert spec.is_off and spec.label == "none"


@pytest.mark.parametrize(
    ("role", "line", "message"),
    [
        ("LLM_ANSWER", "claude-haiku", "has no provider"),
        ("LLM_ANSWER", "", "is empty"),
        ("EMBEDDER", "openai/text-embedding-3-large@big", "positive number"),
        ("NOT_A_ROLE", "local/x", "Unknown role"),
        ("LLM_ANSWER", "openrouter/some model", "spaces"),
    ],
)
def test_bad_lines_fail_with_a_clear_message(role: str, line: str, message: str) -> None:
    with pytest.raises(RoleSpecError, match=message):
        parse_role(role, line)


def test_model_choices_are_split_and_trimmed() -> None:
    assert parse_choices(" openrouter/a/b , anthropic/c ,, ") == ["openrouter/a/b", "anthropic/c"]


def test_settings_reject_a_typo_in_any_role_at_startup() -> None:
    with pytest.raises(ValidationError, match="RERANKER"):
        Settings(_env_file=None, reranker="ms-marco")  # type: ignore[call-arg]


def test_prod_requires_a_real_secret_key() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(_env_file=None, app_env="prod")  # type: ignore[call-arg]
    ok = Settings(_env_file=None, app_env="prod", secret_key="x" * 40)  # type: ignore[call-arg]
    assert ok.secure_cookies is True


def test_secrets_never_show_in_repr() -> None:
    s = Settings(_env_file=None, openrouter_api_key="sk-or-very-secret")  # type: ignore[call-arg]
    assert "very-secret" not in repr(s)
    assert s.secret("openrouter_api_key") == "sk-or-very-secret"


def test_fusion_weights_can_be_overridden_per_query_type() -> None:
    s = Settings(_env_file=None, fusion_weights='{"question": {"dense": 0.9}}')  # type: ignore[call-arg]
    table = s.fusion_weight_table
    assert table["question"]["dense"] == 0.9
    assert table["question"]["sparse"] == 0.85  # untouched values keep their defaults


def test_database_url_is_normalised_for_psycopg() -> None:
    s = Settings(_env_file=None, database_url="postgres://u:p@h:5432/d")  # type: ignore[call-arg]
    assert s.sqlalchemy_url() == "postgresql+psycopg://u:p@h:5432/d"
