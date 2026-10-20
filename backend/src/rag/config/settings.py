"""Typed settings. `.env` is the source of truth; real environment variables override it.

The file is found by walking up from the working directory (so `backend/` and the repository root both work), or
named explicitly with RAG_ENV_FILE. Secrets are SecretStr so they never appear in logs, reprs or API responses.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from rag.config.roles import ROLES, RoleSpec, parse_choices, parse_role


def _find_env_file() -> str | None:
    explicit = os.environ.get("RAG_ENV_FILE")
    if explicit:
        return explicit
    here = Path.cwd()
    for folder in (here, *list(here.parents)[:3]):
        candidate = folder / ".env"
        if candidate.is_file():
            return str(candidate)
    return None


DEFAULT_FUSION_WEIGHTS: dict[str, dict[str, float]] = {
    # query type -> leg weights (dense, sparse, exact). Tuned on the evaluation set; see docs/retrieval.md.
    "identifier": {"dense": 0.25, "sparse": 0.35, "exact": 0.40},
    "keyword": {"dense": 0.10, "sparse": 0.90, "exact": 0.30},
    # Dense similarity cannot tell near-identical clauses apart (same template, other project or level); the words can.
    "question": {"dense": 0.15, "sparse": 0.85, "exact": 0.25},
    "non_english": {"dense": 0.70, "sparse": 0.30, "exact": 0.25},
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_find_env_file(), env_file_encoding="utf-8", extra="ignore")

    # ---- application -------------------------------------------------------------------------------------------
    app_env: Literal["dev", "test", "prod"] = "dev"
    app_name: str = "RAG"
    public_url: str = "http://localhost:3000"
    # Container default; only the edge proxy can reach the API (compose does not publish its port).
    api_host: str = "0.0.0.0"  # noqa: S104  # nosec B104
    api_port: int = 8000
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "text"
    secret_key: SecretStr = SecretStr("dev-only-change-me-dev-only-change-me")

    # ---- provider keys and endpoints ---------------------------------------------------------------------------
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_zdr: bool = True
    openrouter_site_url: str = ""
    openrouter_app_name: str = "RAG"
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    azure_openai_api_key: SecretStr | None = None
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2024-10-21"
    openai_compat_base_url: str = ""
    openai_compat_api_key: SecretStr | None = None
    cohere_api_key: SecretStr | None = None
    cohere_base_url: str = "https://api.cohere.com"
    llama_cloud_api_key: SecretStr | None = None
    llama_cloud_base_url: str = "https://api.cloud.llamaindex.ai"
    azure_di_endpoint: str = ""
    azure_di_key: SecretStr | None = None
    mistral_api_key: SecretStr | None = None
    mistral_base_url: str = "https://api.mistral.ai"

    # ---- local model server ------------------------------------------------------------------------------------
    local_models_url: str = "http://localhost:8080"
    local_models: str = "multilingual-e5-small,mmarco-mMiniLMv2-L12"
    local_models_token: SecretStr | None = None
    local_models_timeout_s: float = 120.0

    # ---- roles ---------------------------------------------------------------------------------------------------
    llm_answer: str = "openrouter/anthropic/claude-haiku-5.5"
    llm_answer_hard: str = "openrouter/anthropic/claude-sonnet-5.5"
    llm_answer_fallback: str = "openrouter/anthropic/claude-sonnet-5.5"
    llm_router: str = "openrouter/anthropic/claude-haiku-5.5"
    llm_enrich: str = "openrouter/anthropic/claude-haiku-5.5"
    vision: str = "openrouter/anthropic/claude-haiku-5.5"
    embedder: str = "local/multilingual-e5-small"
    reranker: str = "local/mmarco-mMiniLMv2-L12"
    groundedness: str = "builtin/lexical"
    parser: str = "local/pdfium"
    ocr: str = "local/rapidocr"

    llm_answer_max_tokens: int = 6000
    llm_temperature: float = 0.1
    llm_timeout_s: float = 90.0
    llm_router_timeout_s: float = 3.0
    llm_max_retries: int = 2
    embedder_batch: int = 32
    # requests to the model server in flight at once per process; a big upload otherwise sends 8 or more at once, the CPU-bound
    # server cannot answer them inside the timeout and every one fails and is retried
    embedder_concurrency: int = 2
    reranker_languages: str = "*"  # mmarco-mMiniLMv2 reads Arabic and English; set "en" with the English-only MiniLM
    model_choices: str = ""
    # Let people pick any model OpenRouter serves (listed from its public catalogue), not only those in MODEL_CHOICES. Spend is
    # still bounded by DAILY_BUDGET_USD and by your OpenRouter account limits.
    allow_any_openrouter_model: bool = True
    # Which OpenRouter models the pickers list and the server accepts: comma-separated ids, `free` for every free model, `*` for all.
    openrouter_models: str = "anthropic/claude-haiku-5.5,openai/gpt-4o-mini,free"
    allow_ui_model_switch: bool = True
    show_model_badge: bool = True
    model_prices_file: str = ""

    # ---- storage -------------------------------------------------------------------------------------------------
    database_url: SecretStr = SecretStr("postgresql://rag:rag@localhost:5432/rag")
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pgbouncer: bool = False
    db_statement_timeout_ms: int = 30000
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection_prefix: str = "chunks"
    qdrant_alias: str = "chunks_live"
    qdrant_shards: int = 1
    qdrant_replicas: int = 1
    qdrant_on_disk: bool = True
    qdrant_quantization: Literal["int8", "none"] = "int8"
    qdrant_timeout_s: float = 30.0
    object_store_url: str = "file://./data/originals"
    s3_endpoint_url: str = ""
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_region: str = "us-east-1"
    cache_dir: str = "./data/cache"

    # ---- authentication and security ---------------------------------------------------------------------------
    auth_mode: Literal["local", "entra", "both"] = "local"
    session_ttl_hours: int = 12
    session_idle_minutes: int = 120
    cookie_secure: bool | None = None
    allowed_origins: str = "http://localhost:3000"
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: SecretStr | None = None
    entra_admin_group_id: str = ""
    entra_authority: str = "https://login.microsoftonline.com"
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    principal_sync_minutes: int = 60
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: SecretStr | None = None
    upload_max_mb: int = 200
    clamav_url: str = ""
    rate_limit_chat_per_min: int = 20
    rate_limit_uploads_per_hour: int = 200
    login_attempts_per_15min: int = 30  # failed sign-ins per client address; accounts also lock after 8
    guardrails: Literal["builtin", "llm_guard", "off"] = "builtin"
    mask_pii_in_logs: bool = True
    # Bank accounts, IBANs, card numbers, SWIFT/BIC, sort and routing codes and national ids never reach an outside model: they
    # are replaced by placeholders in every request to a cloud chat model and put back into the reply here, and a page that
    # contains one is not sent to a cloud vision model as an image (see docs/security.md).
    privacy_mask_for_models: bool = True
    privacy_kinds: str = ("iban,card,account,swift,ssn,emirates_id,passport,payment_ref,secret,tax_id,email,phone,long_number,custom")
    # Your own additions: labels whose value is always masked ("Salary, Visa number" masks the text after "Salary:"), and regular
    # expressions separated by ";;" (for example "EMP-\d{5};;PRJ[A-Z]{3}\d{4}"). No restart of the data is needed, only of the services.
    privacy_extra_labels: str = ""
    privacy_extra_patterns: str = ""
    # true removes the values from the stored text for good (they cannot then be found or shown), see docs/security.md
    privacy_redact_stored: bool = False
    # true treats a document that holds any of these values as local-only on its own: nothing from it goes to a cloud model, not
    # even masked (a bank statement also carries names, references and balances that no pattern can mark)
    privacy_auto_local_only: bool = False

    # ---- monitoring ------------------------------------------------------------------------------------------------
    monitor_retention_days: int = 14
    metric_retention_days: int = 365
    alert_webhook_url: str = ""
    alert_webhook_kind: Literal["teams", "slack", "generic"] = "generic"
    prometheus_metrics: bool = False
    daily_budget_usd: float = 10.0

    # ---- retrieval -------------------------------------------------------------------------------------------------
    dense_top_k: int = 60
    sparse_top_k: int = 60
    exact_top_k: int = 30
    # Candidates read by the cross-encoder. Measured with mmarco-mMiniLMv2 on 143 hard questions: 10-20 give identical results
    # (page@1 90.5, page@3 98.3), 40 gives nothing more, 8 starts to lose page@3; the cost is linear in this number.
    rerank_top_n: int = 15
    # How much of the final score the cross-encoder decides (the rest is the fused retrieval score). A cross-encoder alone
    # reads keyword and identifier queries badly, so it is mixed in, and skipped for short or identifier questions.
    rerank_blend: float = 0.8
    # The share above is the most the cross-encoder gets. With `rerank_adaptive` it falls towards `rerank_blend_floor` the more
    # of the question's word pairs a top candidate quotes verbatim (a copied sentence needs no second opinion; a paraphrase does).
    # Measured on 407 questions: no reranker 93.7 / 97.6 / 0.958 (page@1 / page@3 / MRR), this setting 95.0 / 98.4 / 0.967;
    # paraphrase questions 73.5 -> 82.3 page@1, table questions 54.5 -> 72.7. Fixed shares from 0.3 to 0.8 are within two questions.
    rerank_adaptive: bool = True
    rerank_blend_floor: float = 0.2
    rerank_adapt_power: float = 0.5
    rerank_min_words: int = 5
    rerank_skip_identifiers: bool = True
    rerank_cache_size: int = 50000
    # Gate for "nothing in the library answers this": a question is turned away when every enabled signal is weak (0 = off).
    min_dense: float = 0.0
    min_sparse: float = 0.0
    min_coverage: float = 0.0
    # Probability (0..1) of the cross-encoder's best score below which a question is turned away; only for questions that were
    # reranked and are in English/Latin script. Unlike cosine and BM25 magnitudes this means the same on any library. Measured
    # (400 answerable, 57 unanswerable questions): 0.02 turns away 65% of the unanswerable ones (80% of the clearly off-topic,
    # 40% of those sharing vocabulary with the library) and wrongly 0.5% of the answerable ones (0 of 34 paraphrases, 0 of
    # 108 quoted sentences). Cosine alone caught 53%, BM25 25%, word coverage 5% at the same loss.
    min_rerank: float = 0.02
    # Lift a chunk by the best other chunk on its page. Off by default: measured on 407 questions it LOWERED the first-rank page
    # rate (89.5 -> 93.2 with it off, 61.8 -> 76.5 on paraphrases) because it favours pages with several mediocre matches over
    # the page with the one chunk that answers.
    page_boost: float = 0.0
    # Words of the question that stand next to each other, in the same order, in a chunk are strong evidence that it is the
    # passage meant: the share of the question's word pairs found in a chunk is added (times this) to its score.
    # Measured 0.25 -> 0.5: 93.2 -> 93.7 first-rank pages overall, noisy questions 94.4 -> 97.2.
    phrase_boost: float = 0.5
    exact_search_below: int = 40000
    # int8 vectors are searched first and the best candidates re-scored with the full vectors; oversampling is how many
    # times the requested number of candidates is taken from the compressed copy (measured: no rescoring loses ~5 points of
    # recall, rescoring with 2x oversampling about 1).
    qdrant_oversampling: float = 2.0
    # Keep each chunk's text and header in the vector index payload. Off: the index holds only vectors and filter fields
    # (about half the size at 5 TB) and the few candidates that are reranked or shown are read back from PostgreSQL.
    store_text_in_index: bool = True
    candidate_docs: int = 40
    evidence_passages: int = 12
    evidence_per_document: int = 3
    family_demotion: float = 0.6
    name_boost: float = 1.0  # added to every chunk of a file whose name the question spells out
    fusion_weights: str = ""
    agent_max_steps: int = 4
    agent_whole_doc_chars: int = 48_000  # a list-everything question about one spreadsheet reads all of it when it fits this
    agent_max_pages: int = 30
    min_evidence_score: float = 0.05

    # ---- ingestion -------------------------------------------------------------------------------------------------
    chunk_tokens: int = 380
    chunk_header_tokens: int = 64
    chunk_overlap: float = 0.15
    chunk_min_tokens: int = 48
    chunker_version: str = "c4"
    analyzer_version: str = "bm25v2"
    max_file_mb: int = 2048
    ocr_render_dpi: int = 200
    # Longest-edge budget for OCR renders: OCR cost grows with pixels, and an A0 sheet at 200 dpi is ~70 megapixels.
    ocr_max_pixels: int = 8_000_000
    ocr_min_text_chars: int = 40
    # Pictures covering at least this share of a page are read with OCR even when the page has a text layer
    ocr_image_min_fraction: float = 0.04
    # PDF structure (headings, title, running headers): off | fast (typography only) | deep (layout engine for documents
    # of at least PDF_LAYOUT_MIN_PAGES pages, typography for the rest). The layout engine costs about 0.2-0.5 s a page.
    pdf_layout: Literal["off", "fast", "deep"] = "deep"
    pdf_layout_min_pages: int = 5
    pdf_layout_max_pages: int = 600
    pdf_layout_timeout_s: float = 240.0
    pdf_layout_processes: int = 1
    vision_pages_per_day: int = 2000
    cloud_ocr_pages_per_day: int = 20000
    path_rules_file: str = ""
    known_projects: str = ""
    worker_concurrency: int = 4
    job_lease_seconds: int = 300
    job_max_attempts: int = 6
    keep_done_jobs_days: int = 3

    # ------------------------------------------------------------------------------------------------------------------
    @field_validator("cookie_secure", mode="before")
    @classmethod
    def _empty_cookie_secure(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_range(cls, value: float) -> float:
        if not 0 <= value < 0.5:
            raise ValueError("CHUNK_OVERLAP must be between 0 and 0.5")
        return value

    @model_validator(mode="after")
    def _validate_roles(self) -> Settings:
        # Parse every role line once so a typo fails at startup with a precise message, not at the first request.
        for role in ROLES:
            parse_role(role, getattr(self, role.lower()))
        for choice in parse_choices(self.model_choices):
            parse_role("LLM_ANSWER", choice)
        if self.app_env == "prod":
            if self.secret_key.get_secret_value().startswith("dev-only"):
                raise ValueError("SECRET_KEY must be set to a long random value when APP_ENV=prod")
            if len(self.secret_key.get_secret_value()) < 32:
                raise ValueError("SECRET_KEY must be at least 32 characters")
        return self

    # ---- derived values --------------------------------------------------------------------------------------------
    def role(self, name: str) -> RoleSpec:
        return parse_role(name, getattr(self, name.lower()))

    def roles(self) -> dict[str, RoleSpec]:
        return {name: self.role(name) for name in ROLES}

    @property
    def privacy_kind_list(self) -> tuple[str, ...]:
        return tuple(k.strip() for k in self.privacy_kinds.split(",") if k.strip())

    @property
    def model_choice_lines(self) -> list[str]:
        return parse_choices(self.model_choices)

    @property
    def reranker_language_set(self) -> set[str] | None:
        values = {v.strip().lower() for v in self.reranker_languages.split(",") if v.strip()}
        return None if "*" in values or "all" in values else values

    @property
    def allowed_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.app_env == "prod"

    @property
    def local_model_list(self) -> list[str]:
        return [m.strip() for m in self.local_models.split(",") if m.strip()]

    @property
    def known_project_list(self) -> list[str]:
        return [p.strip() for p in self.known_projects.split(",") if p.strip()]

    @property
    def fusion_weight_table(self) -> dict[str, dict[str, float]]:
        table = {k: dict(v) for k, v in DEFAULT_FUSION_WEIGHTS.items()}
        if self.fusion_weights.strip():
            override = json.loads(self.fusion_weights)
            for query_type, weights in override.items():
                table.setdefault(query_type, {}).update({k: float(v) for k, v in weights.items()})
        return table

    def sqlalchemy_url(self, *, async_driver: bool = True) -> str:
        url = self.database_url.get_secret_value()
        for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgresql+asyncpg://", "postgres://"):
            if url.startswith(prefix):
                url = "postgresql://" + url[len(prefix):]
        return url.replace("postgresql://", "postgresql+psycopg://", 1) if async_driver else url

    def secret(self, name: str) -> str | None:
        value = getattr(self, name, None)
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value.get_secret_value() or None
        return str(value) or None


class _SettingsHolder:
    override: Settings | None = None


@lru_cache(maxsize=1)
def _load() -> Settings:
    return Settings()


def get_settings() -> Settings:
    if _SettingsHolder.override is not None:
        return _SettingsHolder.override
    return _load()


def set_settings(settings: Settings | None) -> None:
    """Replace the process settings (tests, CLI flags). Pass None to go back to reading the environment."""
    _SettingsHolder.override = settings
    _load.cache_clear()

