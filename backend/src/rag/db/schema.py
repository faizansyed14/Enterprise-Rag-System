"""Database schema (SQLAlchemy Core). Alembic migrations are generated from this file and checked against it in tests.

Large tables are partitioned so they stay fast at multi-terabyte scale:
  pages, chunks          HASH partitions (16) on the content/version key -- even spread, partition-wise lookups
  traces, spans          RANGE partitions by day -- old days are dropped instead of deleted row by row
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING)

HASH_PARTITIONS = 16
TS = TIMESTAMP(timezone=True)


def _uuid_pk() -> Column:
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _created() -> Column:
    return Column("created_at", TS, nullable=False, server_default=func.now())


# ---- people and access -----------------------------------------------------------------------------------------------

users = Table(
    "users",
    metadata,
    _uuid_pk(),
    Column("email", Text, nullable=False),
    Column("display_name", Text, nullable=False, server_default=""),
    Column("password_hash", Text),
    Column("auth_provider", Text, nullable=False, server_default="local"),
    Column("external_id", Text),
    Column("is_admin", Boolean, nullable=False, server_default=text("false")),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    _created(),
    Column("last_login_at", TS),
    Column("failed_logins", Integer, nullable=False, server_default="0"),
    Column("locked_until", TS),
    Column("principals_synced_at", TS),
)
Index("uq_users_email_lower", func.lower(users.c.email), unique=True)
Index("uq_users_external_id", users.c.external_id, unique=True, postgresql_where=users.c.external_id.isnot(None))

sessions = Table(
    "sessions",
    metadata,
    Column("token_hash", Text, primary_key=True),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("csrf_token", Text, nullable=False),
    _created(),
    Column("expires_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False, server_default=func.now()),
    Column("ip", Text),
    Column("user_agent", Text),
)

user_principals = Table(
    "user_principals",
    metadata,
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("principal", Text, nullable=False),
    Column("source", Text, nullable=False),  # self | entra | site_group | local
    Column("synced_at", TS, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("user_id", "principal"),
)
Index("ix_user_principals_principal", user_principals.c.principal)

site_groups = Table(
    "site_groups",
    metadata,
    Column("site_id", Text, nullable=False),
    Column("group_id", Text, nullable=False),
    Column("title", Text, nullable=False, server_default=""),
    Column("members", ARRAY(Text), nullable=False, server_default="{}"),
    Column("synced_at", TS, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("site_id", "group_id"),
)
Index("ix_site_groups_members", site_groups.c.members, postgresql_using="gin")

# ---- sources and content ---------------------------------------------------------------------------------------------

sources = Table(
    "sources",
    metadata,
    _uuid_pk(),
    Column("name", Text, nullable=False, unique=True),
    Column("kind", Text, nullable=False),  # upload | filesystem | smb | sharepoint | onedrive
    Column("config", JSONB, nullable=False, server_default="{}"),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("schedule_minutes", Integer),
    Column("default_acl", ARRAY(Text), nullable=False, server_default="{}"),
    Column("cursor", JSONB, nullable=False, server_default="{}"),
    Column("last_sync_started_at", TS),
    Column("last_sync_finished_at", TS),
    Column("last_sync_status", Text),
    Column("last_sync_error", Text),
    Column("stats", JSONB, nullable=False, server_default="{}"),
    _created(),
    Column("created_by", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
)

blobs = Table(
    "blobs",
    metadata,
    Column("sha256", Text, primary_key=True),
    Column("size", BigInteger, nullable=False),
    Column("mime", Text, nullable=False, server_default="application/octet-stream"),
    Column("format", Text),
    Column("storage_key", Text, nullable=False),
    _created(),
    Column("extracted_at", TS),
    Column("page_count", Integer),
    Column("extract_method", Text),
    Column("extract_warnings", JSONB, nullable=False, server_default="[]"),
    Column("extract_error", Text),
    Column("properties", JSONB, nullable=False, server_default="{}"),
    Column("attachments", JSONB, nullable=False, server_default="[]"),
)

documents = Table(
    "documents",
    metadata,
    _uuid_pk(),
    Column("doc_key", Text, nullable=False, unique=True),
    Column("doc_number", Text),
    Column("title", Text, nullable=False, server_default=""),
    Column("project_ids", ARRAY(Text), nullable=False, server_default="{}"),
    Column("doc_type", Text),
    Column("spec_section", Text),
    Column("family_id", UUID(as_uuid=True)),
    Column("current_version_id", UUID(as_uuid=True)),
    Column("status", Text, nullable=False, server_default="active"),
    Column("local_only", Boolean, nullable=False, server_default=text("false")),
    _created(),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
)
Index("ix_documents_doc_number", documents.c.doc_number)
Index("ix_documents_project_ids", documents.c.project_ids, postgresql_using="gin")
Index("ix_documents_family_id", documents.c.family_id)
Index("ix_documents_updated_at", documents.c.updated_at)

versions = Table(
    "versions",
    metadata,
    _uuid_pk(),
    Column("document_id", UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
    Column("blob_sha256", Text, ForeignKey("blobs.sha256"), nullable=False),
    Column("title", Text, nullable=False, server_default=""),
    Column("language", Text),
    Column("metadata", JSONB, nullable=False, server_default="{}"),
    Column("identifiers", ARRAY(Text), nullable=False, server_default="{}"),
    Column("state", Text, nullable=False, server_default="pending"),
    Column("index_build_id", UUID(as_uuid=True)),
    Column("chunk_count", Integer, nullable=False, server_default="0"),
    Column("page_count", Integer, nullable=False, server_default="0"),
    Column("error", Text),
    _created(),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
    Column("live_at", TS),
    # MinHash signature of the text and its LSH band keys for near-duplicate lookups (text/minhash.py).
    Column("minhash", ARRAY(Integer)),
    Column("minhash_bands", ARRAY(Integer)),
    UniqueConstraint("document_id", "blob_sha256"),
)
Index("ix_versions_state", versions.c.state)
Index("ix_versions_blob", versions.c.blob_sha256)
Index("ix_versions_identifiers", versions.c.identifiers, postgresql_using="gin")
Index("ix_versions_minhash_bands", versions.c.minhash_bands, postgresql_using="gin")

source_items = Table(
    "source_items",
    metadata,
    _uuid_pk(),
    Column("source_id", UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", Text, nullable=False),
    Column("path", Text, nullable=False, server_default=""),
    Column("name", Text, nullable=False, server_default=""),
    Column("parent_item_id", UUID(as_uuid=True)),
    Column("etag", Text),
    Column("size", BigInteger),
    Column("modified_at", TS),
    Column("mime", Text),
    Column("content_sha256", Text),
    Column("version_id", UUID(as_uuid=True), ForeignKey("versions.id", ondelete="SET NULL")),
    Column("acl", ARRAY(Text), nullable=False, server_default="{}"),
    Column("acl_hash", Text),
    Column("fields", JSONB, nullable=False, server_default="{}"),
    Column("status", Text, nullable=False, server_default="active"),  # active | deleted | skipped | error
    Column("skip_reason", Text),
    Column("error", Text),
    Column("first_seen_at", TS, nullable=False, server_default=func.now()),
    Column("last_seen_at", TS, nullable=False, server_default=func.now()),
    Column("fetched_at", TS),
    UniqueConstraint("source_id", "external_id"),
)
Index("ix_source_items_version_id", source_items.c.version_id)
Index("ix_source_items_source_status", source_items.c.source_id, source_items.c.status)
Index("ix_source_items_sha", source_items.c.content_sha256)

version_access = Table(
    "version_access",
    metadata,
    Column("version_id", UUID(as_uuid=True), ForeignKey("versions.id", ondelete="CASCADE"), nullable=False),
    Column("principal", Text, nullable=False),
    PrimaryKeyConstraint("version_id", "principal"),
)
Index("ix_version_access_principal", version_access.c.principal)

pages = Table(
    "pages",
    metadata,
    Column("blob_sha256", Text, nullable=False),
    Column("page_no", Integer, nullable=False),
    Column("text", Text, nullable=False, server_default=""),
    Column("method", Text, nullable=False),
    Column("language", Text),
    Column("label", Text),
    Column("width", Float),
    Column("height", Float),
    Column("is_drawing", Boolean, nullable=False, server_default=text("false")),
    Column("title_block", JSONB, nullable=False, server_default="{}"),
    Column("tables", JSONB, nullable=False, server_default="[]"),
    Column("lines", JSONB, nullable=False, server_default="[]"),
    Column("quality", JSONB, nullable=False, server_default="{}"),
    Column("image_hash", Text),
    _created(),
    PrimaryKeyConstraint("blob_sha256", "page_no"),
    postgresql_partition_by="HASH (blob_sha256)",
)

ocr_cache = Table(
    "ocr_cache",
    metadata,
    Column("image_hash", Text, nullable=False),
    Column("engine", Text, nullable=False),
    Column("text", Text, nullable=False, server_default=""),
    Column("lines", JSONB, nullable=False, server_default="[]"),
    Column("confidence", Float, nullable=False, server_default="0"),
    _created(),
    PrimaryKeyConstraint("image_hash", "engine"),
)

chunks = Table(
    "chunks",
    metadata,
    Column("version_id", UUID(as_uuid=True), nullable=False),
    Column("chunk_no", Integer, nullable=False),
    Column("id", UUID(as_uuid=True), nullable=False),
    Column("document_id", UUID(as_uuid=True), nullable=False),
    Column("page_start", Integer, nullable=False),
    Column("page_end", Integer, nullable=False),
    Column("section_path", Text, nullable=False, server_default=""),
    Column("kind", Text, nullable=False, server_default="text"),
    Column("header", Text, nullable=False, server_default=""),
    Column("text", Text, nullable=False),
    Column("token_count", Integer, nullable=False, server_default="0"),
    Column("language", Text),
    Column("content_hash", Text, nullable=False),
    PrimaryKeyConstraint("version_id", "chunk_no"),
    postgresql_partition_by="HASH (version_id)",
)
Index("ix_chunks_document_id", chunks.c.document_id)

doc_references = Table(
    "doc_references",
    metadata,
    Column("from_version_id", UUID(as_uuid=True), ForeignKey("versions.id", ondelete="CASCADE"), nullable=False),
    Column("target", Text, nullable=False),
    Column("kind", Text, nullable=False, server_default="refers_to"),
    Column("target_document_id", UUID(as_uuid=True)),
    Column("page_no", Integer),
    Column("context", Text, nullable=False, server_default=""),
    PrimaryKeyConstraint("from_version_id", "target", "kind"),
)
Index("ix_doc_references_target", doc_references.c.target)

# ---- conversations ---------------------------------------------------------------------------------------------------

conversations = Table(
    "conversations",
    metadata,
    _uuid_pk(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("title", Text, nullable=False, server_default="New conversation"),
    Column("filters", JSONB, nullable=False, server_default="{}"),
    Column("archived", Boolean, nullable=False, server_default=text("false")),
    _created(),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
)
Index("ix_conversations_user_updated", conversations.c.user_id, conversations.c.updated_at)

messages = Table(
    "messages",
    metadata,
    _uuid_pk(),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False, server_default=""),
    Column("citations", JSONB, nullable=False, server_default="[]"),
    Column("filters", JSONB, nullable=False, server_default="{}"),
    Column("model", Text),
    Column("trace_id", Text),
    Column("status", Text, nullable=False, server_default="ok"),
    Column("groundedness", Float),
    _created(),
    Column("feedback_score", SmallInteger),
    Column("feedback_comment", Text),
    Column("feedback_at", TS),
)
Index("ix_messages_conversation_created", messages.c.conversation_id, messages.c.created_at)

# ---- jobs ------------------------------------------------------------------------------------------------------------

jobs = Table(
    "jobs",
    metadata,
    Column("id", BigInteger, Identity(always=False), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("lane", Text, nullable=False),
    Column("lane_rank", SmallInteger, nullable=False),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("payload", JSONB, nullable=False, server_default="{}"),
    Column("dedupe_key", Text),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="6"),
    Column("run_after", TS, nullable=False, server_default=func.now()),
    Column("leased_by", Text),
    Column("lease_expires_at", TS),
    Column("heartbeat_at", TS),
    Column("last_error", Text),
    Column("error_class", Text),
    Column("rate_limited", Integer, nullable=False, server_default="0"),
    Column("progress", JSONB, nullable=False, server_default="{}"),
    Column("source_id", UUID(as_uuid=True)),
    Column("version_id", UUID(as_uuid=True)),
    Column("trace_id", Text),
    _created(),
    Column("started_at", TS),
    Column("finished_at", TS),
)
Index(
    "ix_jobs_ready",
    jobs.c.lane_rank,
    jobs.c.priority.desc(),
    jobs.c.run_after,
    jobs.c.id,
    postgresql_where=jobs.c.status == "queued",
)
Index(
    "uq_jobs_dedupe_active",
    jobs.c.dedupe_key,
    unique=True,
    postgresql_where=text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"),
)
Index("ix_jobs_running_lease", jobs.c.lease_expires_at, postgresql_where=jobs.c.status == "running")
Index("ix_jobs_status_kind", jobs.c.status, jobs.c.kind)
Index("ix_jobs_version_id", jobs.c.version_id)
Index("ix_jobs_source_status", jobs.c.source_id, jobs.c.status)
Index("ix_jobs_finished_at", jobs.c.finished_at, postgresql_where=jobs.c.status.in_(["done", "cancelled"]))

provider_cooldowns = Table(
    "provider_cooldowns",
    metadata,
    Column("provider", Text, primary_key=True),
    Column("until", TS, nullable=False),
    Column("reason", Text, nullable=False, server_default=""),
)

daily_budgets = Table(
    "daily_budgets",
    metadata,
    Column("day", Date, nullable=False),
    Column("bucket", Text, nullable=False),  # vision_pages | cloud_ocr_pages
    Column("used", Integer, nullable=False, server_default="0"),
    PrimaryKeyConstraint("day", "bucket"),
)

# ---- index builds and model choices ----------------------------------------------------------------------------------

index_builds = Table(
    "index_builds",
    metadata,
    _uuid_pk(),
    Column("collection", Text, nullable=False, unique=True),
    Column("embedder", Text, nullable=False),
    Column("embedder_dims", Integer, nullable=False),
    Column("analyzer", Text, nullable=False),
    Column("chunker_version", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="building"),  # building | ready | live | retired | failed
    _created(),
    Column("live_at", TS),
    Column("retired_at", TS),
    Column("total_versions", Integer, nullable=False, server_default="0"),
    Column("done_versions", Integer, nullable=False, server_default="0"),
    Column("notes", Text, nullable=False, server_default=""),
)

role_overrides = Table(
    "role_overrides",
    metadata,
    Column("role", Text, primary_key=True),
    Column("spec", Text, nullable=False),
    Column("updated_by", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
)

model_switches = Table(
    "model_switches",
    metadata,
    Column("id", BigInteger, Identity(always=False), primary_key=True),
    Column("role", Text, nullable=False),
    Column("from_spec", Text, nullable=False),
    Column("to_spec", Text, nullable=False),
    Column("switched_by", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    Column("switched_at", TS, nullable=False, server_default=func.now()),
    Column("reason", Text, nullable=False, server_default=""),
)

# ---- monitoring ------------------------------------------------------------------------------------------------------

traces = Table(
    "traces",
    metadata,
    Column("id", Text, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("user_id", UUID(as_uuid=True)),
    Column("status", Text, nullable=False, server_default="ok"),
    Column("duration_ms", Float),
    Column("attrs", JSONB, nullable=False, server_default="{}"),
    PrimaryKeyConstraint("id", "started_at"),
    postgresql_partition_by="RANGE (started_at)",
)
Index("ix_traces_started_at", traces.c.started_at)

spans = Table(
    "spans",
    metadata,
    Column("id", Text, nullable=False),
    Column("trace_id", Text),
    Column("parent_id", Text),
    Column("started_at", TS, nullable=False),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("role", Text),
    Column("provider", Text),
    Column("model", Text),
    Column("duration_ms", Float, nullable=False),
    Column("status", Text, nullable=False),
    Column("error", Text),
    Column("tokens_in", Integer, nullable=False, server_default="0"),
    Column("tokens_out", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Float),
    Column("items", Integer, nullable=False, server_default="1"),
    Column("attrs", JSONB, nullable=False, server_default="{}"),
    PrimaryKeyConstraint("id", "started_at"),
    postgresql_partition_by="RANGE (started_at)",
)
Index("ix_spans_trace_id", spans.c.trace_id)
Index("ix_spans_started_at", spans.c.started_at)

metric_minutes = Table(
    "metric_minutes",
    metadata,
    Column("minute", TS, nullable=False),
    Column("kind", Text, nullable=False),
    Column("role", Text, nullable=False, server_default=""),
    Column("provider", Text, nullable=False, server_default=""),
    Column("model", Text, nullable=False, server_default=""),
    Column("calls", Integer, nullable=False, server_default="0"),
    Column("errors", Integer, nullable=False, server_default="0"),
    Column("total_ms", Float, nullable=False, server_default="0"),
    Column("max_ms", Float, nullable=False, server_default="0"),
    Column("buckets", ARRAY(Integer), nullable=False, server_default="{}"),
    Column("tokens_in", BigInteger, nullable=False, server_default="0"),
    Column("tokens_out", BigInteger, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=False, server_default="0"),
    PrimaryKeyConstraint("minute", "kind", "role", "provider", "model"),
)

process_stats = Table(
    "process_stats",
    metadata,
    Column("process_id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("host", Text, nullable=False),
    Column("pid", Integer, nullable=False),
    Column("version", Text, nullable=False, server_default=""),
    Column("started_at", TS, nullable=False),
    Column("heartbeat_at", TS, nullable=False),
    Column("stats", JSONB, nullable=False, server_default="{}"),
)

usage_daily = Table(
    "usage_daily",
    metadata,
    Column("day", Date, nullable=False),
    Column("user_key", Text, nullable=False),  # user id, or "system" for ingestion
    Column("role", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("calls", Integer, nullable=False, server_default="0"),
    Column("tokens_in", BigInteger, nullable=False, server_default="0"),
    Column("tokens_out", BigInteger, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=False, server_default="0"),
    PrimaryKeyConstraint("day", "user_key", "role", "model"),
)

alerts = Table(
    "alerts",
    metadata,
    Column("id", BigInteger, Identity(always=False), primary_key=True),
    Column("rule", Text, nullable=False),
    Column("key", Text, nullable=False, server_default=""),
    Column("severity", Text, nullable=False),
    Column("message", Text, nullable=False),
    Column("details", JSONB, nullable=False, server_default="{}"),
    Column("first_seen_at", TS, nullable=False, server_default=func.now()),
    Column("last_seen_at", TS, nullable=False, server_default=func.now()),
    Column("resolved_at", TS),
    Column("notified_at", TS),
    Column("count", Integer, nullable=False, server_default="1"),
)
Index("uq_alerts_open", alerts.c.rule, alerts.c.key, unique=True, postgresql_where=alerts.c.resolved_at.is_(None))

rate_limits = Table(
    "rate_limits",
    metadata,
    Column("key", Text, nullable=False),
    Column("window_start", TS, nullable=False),
    Column("count", Integer, nullable=False, server_default="0"),
    PrimaryKeyConstraint("key", "window_start"),
)

# ---- evaluation and audit --------------------------------------------------------------------------------------------

eval_runs = Table(
    "eval_runs",
    metadata,
    _uuid_pk(),
    Column("name", Text, nullable=False),
    Column("golden_set", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("config", JSONB, nullable=False, server_default="{}"),
    Column("metrics", JSONB, nullable=False, server_default="{}"),
    Column("error", Text),
    Column("created_by", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    _created(),
    Column("started_at", TS),
    Column("finished_at", TS),
)

eval_items = Table(
    "eval_items",
    metadata,
    Column("run_id", UUID(as_uuid=True), ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False),
    Column("item_id", Text, nullable=False),
    Column("question", Text, nullable=False),
    Column("expected", JSONB, nullable=False, server_default="{}"),
    Column("retrieved", JSONB, nullable=False, server_default="[]"),
    Column("answer", Text),
    Column("metrics", JSONB, nullable=False, server_default="{}"),
    Column("latency_ms", Float),
    PrimaryKeyConstraint("run_id", "item_id"),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, Identity(always=False), primary_key=True),
    Column("at", TS, nullable=False, server_default=func.now()),
    Column("user_id", UUID(as_uuid=True)),
    Column("action", Text, nullable=False),
    Column("target", Text, nullable=False, server_default=""),
    Column("ip", Text),
    Column("details", JSONB, nullable=False, server_default="{}"),
)
Index("ix_audit_log_at", audit_log.c.at)
Index("ix_audit_log_user_at", audit_log.c.user_id, audit_log.c.at)

PARTITIONED_HASH = {"pages": HASH_PARTITIONS, "chunks": HASH_PARTITIONS}
PARTITIONED_DAILY = ("traces", "spans")
