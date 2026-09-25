"""sources domain: topics, sources, versions, chunks (FTS), chat, summaries, jobs

Mirrors ``app.domain.models`` exactly — the ``compare_metadata()`` test keeps the two in sync.

Revision ID: 0002_sources_domain
Revises: 0001_core_baseline
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_sources_domain"
down_revision: str | None = "0001_core_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_NOW = sa.text("now()")
_GEN_UUID = sa.text("gen_random_uuid()")


def _id() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True, server_default=_GEN_UUID)


def _user() -> sa.Column:
    return sa.Column(
        "user_id", _UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


def _ts(name: str, nullable: bool = False, default: bool = True) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=_NOW if default else None,
    )


def _uuid_array(name: str) -> sa.Column:
    return sa.Column(name, postgresql.ARRAY(_UUID), nullable=False, server_default=sa.text("'{}'"))


def _idem_index(table: str) -> None:
    op.create_index(
        f"ux_{table}_idempotency",
        table,
        ["user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


_RUN_STATUSES = "status IN ('queued', 'running', 'succeeded', 'failed', 'blocked', 'canceled')"


def upgrade() -> None:
    op.create_table(
        "topics",
        _id(),
        _user(),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_is_auto", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("deleted_at", nullable=True, default=False),
    )
    op.create_index("ix_topics_user_updated", "topics", ["user_id", "updated_at"])
    _idem_index("topics")

    op.create_table(
        "sources",
        _id(),
        sa.Column(
            "topic_id", _UUID, sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
        ),
        _user(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("file_key", sa.Text(), nullable=True),
        sa.Column("file_mime", sa.Text(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("content_sha256", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column("current_version_id", _UUID, nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("deleted_at", nullable=True, default=False),
        sa.CheckConstraint("kind IN ('pdf', 'text', 'web')", name="ck_sources_kind"),
        sa.CheckConstraint(
            "status IN ('uploading', 'extracting', 'indexing', 'ready', 'failed')",
            name="ck_sources_status",
        ),
    )
    op.create_index("ix_sources_topic", "sources", ["topic_id", "created_at"])
    op.create_index("ix_sources_user", "sources", ["user_id"])
    _idem_index("sources")

    op.create_table(
        "source_versions",
        _id(),
        sa.Column(
            "source_id", _UUID, sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
        ),
        _user(),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("text_sha256", sa.Text(), nullable=False),
        sa.Column("pages", postgresql.JSONB(), nullable=True),
        sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        _ts("created_at"),
        sa.UniqueConstraint("source_id", "version_no", name="uq_source_versions_no"),
    )

    op.create_table(
        "chunks",
        _id(),
        sa.Column(
            "version_id",
            _UUID,
            sa.ForeignKey("source_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_id", _UUID, sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
        ),
        _user(),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("end", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('russian', text) || to_tsvector('english', text)", persisted=True
            ),
        ),
    )
    op.create_index("ix_chunks_version", "chunks", ["version_id", "ordinal"])
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")

    op.create_table(
        "chat_messages",
        _id(),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "topic_id", _UUID, sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
        ),
        _user(),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        _uuid_array("requested_source_ids"),
        _uuid_array("source_version_ids"),
        sa.Column("answer", postgresql.JSONB(), nullable=True),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("block_reason", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("generation_id", _UUID, nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_demo_free", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("completed_at", nullable=True, default=False),
        sa.CheckConstraint(_RUN_STATUSES, name="ck_chat_messages_status"),
    )
    op.create_index("ix_chat_messages_topic_seq", "chat_messages", ["topic_id", "seq"])
    _idem_index("chat_messages")

    op.create_table(
        "summaries",
        _id(),
        sa.Column(
            "topic_id", _UUID, sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
        ),
        _user(),
        sa.Column("status", sa.Text(), nullable=False),
        _uuid_array("requested_source_ids"),
        _uuid_array("source_version_ids"),
        sa.Column("content", postgresql.JSONB(), nullable=True),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("block_reason", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("generation_id", _UUID, nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("completed_at", nullable=True, default=False),
        sa.CheckConstraint(_RUN_STATUSES, name="ck_summaries_status"),
    )
    op.create_index("ix_summaries_topic", "summaries", ["topic_id", "created_at"])
    _idem_index("summaries")

    op.create_table(
        "domain_jobs",
        _id(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _ts("run_after"),
        _ts("locked_until", nullable=True, default=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "kind IN ('ingest', 'answer', 'summary', 'purge_topic', 'purge_source')",
            name="ck_domain_jobs_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name="ck_domain_jobs_status"
        ),
    )
    op.create_index("ix_domain_jobs_pick", "domain_jobs", ["status", "run_after"])
    op.create_index(
        "ux_domain_jobs_active",
        "domain_jobs",
        ["kind", "ref_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "user_domain_state",
        sa.Column(
            "user_id",
            _UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "demo_provisioned", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        _ts("created_at"),
    )


def downgrade() -> None:
    for table in (
        "user_domain_state",
        "domain_jobs",
        "summaries",
        "chat_messages",
        "chunks",
        "source_versions",
        "sources",
        "topics",
    ):
        op.drop_table(table)
