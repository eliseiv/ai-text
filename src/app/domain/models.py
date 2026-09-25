"""ORM of the domain schema (migration ``0002_sources_domain``).

Bound to the core ``Base`` so ``alembic --autogenerate`` and the ``compare_metadata()`` test see
every domain table. Statuses are ``text`` + ``CHECK`` (not PG enums): a new status is a one-line
migration instead of an ``ALTER TYPE`` that cannot run inside a transaction.

Ownership: every row carries ``user_id``. Every read in the repository filters on it — isolation
is a WHERE clause on every query, never an afterthought (a foreign id is a plain 404).

Versions: ``source_versions`` are immutable text snapshots. Answers and summaries store the
version ids they were built from, so a citation always points to the exact text it quoted.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

_uuid_default = sa_text("gen_random_uuid()")
_now = sa_text("now()")

SOURCE_KINDS = ("pdf", "text", "web")
# загрузка → извлечение текста → подготовка к вопросам → готово / ошибка
SOURCE_STATUSES = ("uploading", "extracting", "indexing", "ready", "failed")
# queued → running → succeeded | failed ; blocked = policy said no (nothing charged, retryable
# after a purchase) ; canceled = the user stopped waiting.
RUN_STATUSES = ("queued", "running", "succeeded", "failed", "blocked", "canceled")
JOB_KINDS = ("ingest", "answer", "summary", "purge_topic", "purge_source")
JOB_STATUSES = ("queued", "running", "done", "failed")

# Full-text index over BOTH stemmers: materials are Russian with English quotes (or the reverse).
TSV_EXPRESSION = "to_tsvector('russian', text) || to_tsvector('english', text)"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(nullable: bool = False, default: bool = True) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=_now if default else None,
    )


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # The title was suggested by the server (not typed by the user) and may still be replaced by
    # the first ready source's title.
    title_is_auto: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("false"))
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = _ts()
    updated_at: Mapped[datetime.datetime] = _ts()
    deleted_at: Mapped[datetime.datetime | None] = _ts(nullable=True, default=False)

    __table_args__ = (
        Index("ix_topics_user_updated", "user_id", "updated_at"),
        Index(
            "ux_topics_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sa_text("idempotency_key IS NOT NULL"),
        ),
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Selected for answers/summaries. Default: every ready source is selected.
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("true"))
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # web: the URL the user gave; the fetched final URL + date live on the version snapshot.
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The uploaded/pasted/fetched original in file storage.
    file_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_mime: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa_text("0"))
    content_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = _ts()
    updated_at: Mapped[datetime.datetime] = _ts()
    deleted_at: Mapped[datetime.datetime | None] = _ts(nullable=True, default=False)

    __table_args__ = (
        CheckConstraint(_in("kind", SOURCE_KINDS), name="ck_sources_kind"),
        CheckConstraint(_in("status", SOURCE_STATUSES), name="ck_sources_status"),
        Index("ix_sources_topic", "topic_id", "created_at"),
        Index("ix_sources_user", "user_id"),
        Index(
            "ux_sources_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sa_text("idempotency_key IS NOT NULL"),
        ),
    )


class SourceVersion(Base):
    """An immutable text snapshot of a source. ``pages``: ``[{n, label, start, end}]``."""

    __tablename__ = "source_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    text_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    pages: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime.datetime] = _ts()

    __table_args__ = (UniqueConstraint("source_id", "version_no", name="uq_source_versions_no"),)


class Chunk(Base):
    """A retrieval/citation unit: ``text == version.text[start:end]`` exactly."""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_versions.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed(TSV_EXPRESSION, persisted=True))

    __table_args__ = (
        Index("ix_chunks_version", "version_id", "ordinal"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )


class ChatMessage(Base):
    """One question and its answer (a chat turn). ``status`` tracks the answer."""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    # Monotonic order for cursor pagination (timestamps can tie).
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=False), nullable=False)
    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # The sources the user asked with (explicit or "all selected" at send time).
    requested_source_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa_text("'{}'")
    )
    # What the answer was actually built from (filled when the run starts).
    source_version_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa_text("'{}'")
    )
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    is_demo_free: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = _ts()
    updated_at: Mapped[datetime.datetime] = _ts()
    completed_at: Mapped[datetime.datetime | None] = _ts(nullable=True, default=False)

    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="ck_chat_messages_status"),
        Index("ix_chat_messages_topic_seq", "topic_id", "seq"),
        Index(
            "ux_chat_messages_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sa_text("idempotency_key IS NOT NULL"),
        ),
    )


class Summary(Base):
    __tablename__ = "summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    requested_source_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa_text("'{}'")
    )
    source_version_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa_text("'{}'")
    )
    content: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = _ts()
    updated_at: Mapped[datetime.datetime] = _ts()
    completed_at: Mapped[datetime.datetime | None] = _ts(nullable=True, default=False)

    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="ck_summaries_status"),
        Index("ix_summaries_topic", "topic_id", "created_at"),
        Index(
            "ux_summaries_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sa_text("idempotency_key IS NOT NULL"),
        ),
    )


class DomainJob(Base):
    """Durable work queue (claimed with ``FOR UPDATE SKIP LOCKED``; a lost worker's lease expires
    and the job is picked up again). Only one ACTIVE job per ``(kind, ref_id)``."""

    __tablename__ = "domain_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    ref_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    run_after: Mapped[datetime.datetime] = _ts()
    locked_until: Mapped[datetime.datetime | None] = _ts(nullable=True, default=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = _ts()
    updated_at: Mapped[datetime.datetime] = _ts()

    __table_args__ = (
        CheckConstraint(_in("kind", JOB_KINDS), name="ck_domain_jobs_kind"),
        CheckConstraint(_in("status", JOB_STATUSES), name="ck_domain_jobs_status"),
        Index("ix_domain_jobs_pick", "status", "run_after"),
        Index(
            "ux_domain_jobs_active",
            "kind",
            "ref_id",
            unique=True,
            postgresql_where=sa_text("status IN ('queued', 'running')"),
        ),
    )


class UserDomainState(Base):
    """Per-user flags of the domain (demo topic provisioned / dismissed)."""

    __tablename__ = "user_domain_state"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    demo_provisioned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    created_at: Mapped[datetime.datetime] = _ts()


DOMAIN_TABLES = (
    "domain_jobs",
    "chat_messages",
    "summaries",
    "chunks",
    "source_versions",
    "sources",
    "topics",
    "user_domain_state",
)
