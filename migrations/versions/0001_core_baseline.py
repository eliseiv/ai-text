"""core baseline: 11 tables + 2 views + enums + pgcrypto

The ONE migration of the core. A template is deployed onto an EMPTY database — there is no
upgrade path to preserve — so the source service's 15-migration chain (13 of them wholly or
partly about the discarded domain) is not carried over. Domain migrations start at ``0002``
with ``down_revision = "0001_core_baseline"`` (single head).

Invariant: every table created here has an ORM model in ``src/app/models/tables.py``,
so ``compare_metadata()`` against ``Base.metadata`` is EMPTY and ``alembic --autogenerate``
never emits a destructive ``drop_table``. The two views are created with raw SQL and are not
tracked by Alembic (PostgreSQL reflection lists tables only).

``downgrade()`` drops everything — it exists for the reversibility test, never for prod.

Revision ID: 0001_core_baseline
Revises:
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_core_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("subscription_status", ("active", "expired", "none")),
    ("ledger_tx_type", ("credit", "debit")),
    ("generation_status", ("pending", "running", "succeeded", "failed", "canceled")),
    ("generation_billing_kind", ("none", "credits", "trial", "unbilled")),
    ("payment_channel", ("apple_storekit", "adapty", "cloudpayments")),
    ("payment_kind", ("subscription", "tokens", "subscription_event")),
    ("payment_status", ("received", "granted", "replayed", "no_grant", "rejected")),
)


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created enum type (``create_type=False`` — no implicit CREATE TYPE)."""
    values = next(v for n, v in _ENUMS if n == name)
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")  # gen_random_uuid()

    for name, values in _ENUMS:
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)

    # --- 1. users ---------------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # BR-1: lifetime trial flag, flipped exactly once (atomic UPDATE ... WHERE NOT trial_used).
        sa.Column("trial_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # --- 2. auth_devices --------------------------------------------------------------------
    # deviceId -> userId. The ONLY trusted source of that link for payment webhooks
    # — providers send the DEVICE id in their "user" field.
    op.create_table(
        "auth_devices",
        sa.Column("device_id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_auth_devices_user", "auth_devices", ["user_id"])
    # Functional index for the case-insensitive webhook resolve `WHERE lower(device_id) = :x`.
    # Non-unique on purpose. Without it that predicate cannot use the PK index.
    op.execute("CREATE INDEX ix_auth_devices_lower_device_id ON auth_devices (lower(device_id))")

    # --- 3. auth_refresh_tokens -------------------------------------------------------------
    op.create_table(
        "auth_refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.Text(),
            sa.ForeignKey("auth_devices.device_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # sha256(opaque refresh token) — NEVER the plaintext token.
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),  # single-use rotation
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),  # chain revocation
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ux_refresh_token_hash", "auth_refresh_tokens", ["token_hash"], unique=True)
    op.create_index("ix_refresh_user_device", "auth_refresh_tokens", ["user_id", "device_id"])

    # --- 4. auth_identities -----------------------------------------------------------------
    op.create_table(
        "auth_identities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),  # 'apple' (extensible)
        sa.Column("subject", sa.Text(), nullable=False),  # provider-stable id (apple sub)
        sa.Column("email", sa.Text(), nullable=True),  # optional (private-relay)
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ux_auth_identities_provider_subject",
        "auth_identities",
        ["provider", "subject"],
        unique=True,
    )
    op.create_index("ix_auth_identities_user", "auth_identities", ["user_id"])

    # --- 5. subscriptions -------------------------------------------------------------------
    op.create_table(
        "subscriptions",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "status", _enum("subscription_status"), nullable=False, server_default=sa.text("'none'")
        ),
        sa.Column("plan", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_subscriptions_expires_at", "subscriptions", ["expires_at"])

    # --- 6. wallets -------------------------------------------------------------------------
    op.create_table(
        "wallets",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("balance", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # AC-3: a negative balance is impossible at DB level, not only in WalletService.
        sa.CheckConstraint("balance >= 0", name="ck_wallets_balance_nonneg"),
    )

    # --- 7. ledger_transactions -------------------------------------------------------------
    # ux_ledger_idempotency is THE single idempotency point of money.
    op.create_table(
        "ledger_transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", _enum("ledger_tx_type"), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_ledger_amount_positive"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="ux_ledger_idempotency"),
    )
    op.create_index(
        "ix_ledger_user_created",
        "ledger_transactions",
        ["user_id", sa.text("created_at DESC")],
    )

    # --- 8. generations ---------------------------------------------------------------------
    op.create_table(
        "generations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),  # 'image' | 'text' | 'video' | ...
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "status",
            _enum("generation_status"),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        # usage — tokens (LLM-like domains)
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "cache_read_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "cache_write_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "total_tokens",
            sa.BigInteger(),
            sa.Computed("input_tokens + output_tokens", persisted=True),
            nullable=False,
        ),
        # usage — units (everything else)
        sa.Column("units", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("unit_kind", sa.Text(), nullable=False, server_default=sa.text("'call'")),
        sa.Column("credits_charged", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "billing_kind",
            _enum("generation_billing_kind"),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
        sa.Column(
            "ledger_tx_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ledger_transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("provider_ref", sa.Text(), nullable=True),  # upstream job id (async callback)
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),  # redacted, <= 1 KB
        sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # idempotency anchor of the generation request (step 2)
        sa.UniqueConstraint("user_id", "idempotency_key", name="ux_generations_idempotency"),
        # BR-7: we pay ONLY for success — a provider failure can never be charged.
        sa.CheckConstraint(
            "credits_charged = 0 OR status = 'succeeded'",
            name="ck_generations_charge_only_succeeded",
        ),
        # no money outside the journal
        sa.CheckConstraint(
            "credits_charged = 0 OR ledger_tx_id IS NOT NULL", name="ck_generations_ledger_link"
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded','failed','canceled')) = (completed_at IS NOT NULL)",
            name="ck_generations_completed_at",
        ),
        sa.CheckConstraint(
            "status = 'failed' OR error_code IS NULL", name="ck_generations_error_code"
        ),
    )
    op.create_index(
        "ix_generations_user_created", "generations", ["user_id", sa.text("created_at DESC")]
    )
    op.create_index(
        "ix_generations_kind_created", "generations", ["kind", sa.text("created_at DESC")]
    )
    # partial — cheap inflight lookup (guard step 1.5 / stuck-generation reaper, TD-006)
    op.create_index(
        "ix_generations_inflight",
        "generations",
        ["status", "created_at"],
        postgresql_where=sa.text("status IN ('pending','running')"),
    )
    # partial UNIQUE — an async provider callback cannot apply to two rows
    op.create_index(
        "ux_generations_provider_ref",
        "generations",
        ["provider", "provider_ref"],
        unique=True,
        postgresql_where=sa.text("provider_ref IS NOT NULL"),
    )
    op.create_index(
        "brin_generations_created", "generations", ["created_at"], postgresql_using="brin"
    )

    # --- 9. payments ------------------------------------------------------------------------
    # LAYER 1 (delivery dedup) = ux_payments_channel_external. LAYER 2 (grant idempotency) lives
    # in ledger_transactions. DIFFERENT columns — merging them silently doubles grants.
    op.create_table(
        "payments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", _enum("payment_channel"), nullable=False),
        # StoreKit transactionId | Adapty profile_event_id | broadapps payment_id (from verify)
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("product_id", sa.Text(), nullable=True),  # key of the PRODUCTS map
        sa.Column("kind", _enum("payment_kind"), nullable=False),
        # NEUTRAL start status — never an optimistic 'granted'.
        sa.Column(
            "status", _enum("payment_status"), nullable=False, server_default=sa.text("'received'")
        ),
        sa.Column("credits_granted", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        # INFORMATIONAL only (reconciliation). NEVER the source of credits (BR-8).
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("grant_idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "ledger_tx_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ledger_transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # SANITIZED per-channel projection (no card PII, no bearer) — not the raw body.
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "payload_version", sa.SmallInteger(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("channel", "external_id", name="ux_payments_channel_external"),
        sa.CheckConstraint(
            "credits_granted = 0 OR ledger_tx_id IS NOT NULL", name="ck_payments_grant_link"
        ),
        sa.CheckConstraint(
            "kind = 'subscription_event' OR product_id IS NOT NULL", name="ck_payments_product"
        ),
        sa.CheckConstraint(
            "status <> 'no_grant' OR credits_granted = 0", name="ck_payments_no_grant"
        ),
    )
    op.create_index(
        "ix_payments_user_received", "payments", ["user_id", sa.text("received_at DESC")]
    )
    op.create_index(
        "ix_payments_channel_received", "payments", ["channel", sa.text("received_at DESC")]
    )
    op.create_index(
        "ix_payments_product",
        "payments",
        ["product_id"],
        postgresql_where=sa.text("product_id IS NOT NULL"),
    )

    # --- 10. user_profiles ------------------------------------------------------------------
    op.create_table(
        "user_profiles",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # --- 11. audit_logs ---------------------------------------------------------------------
    # generation_id -> generations (CORE). The source had session_id -> chat_sessions, an FK from
    # the core onto a DOMAIN table; there is no domain FK in the core here.
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "generation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("generations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),  # allowlist; NO secrets
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_audit_user_created", "audit_logs", ["user_id", sa.text("created_at DESC")])
    op.create_index("ix_audit_event_type", "audit_logs", ["event_type", sa.text("created_at DESC")])

    # --- Views (generation aggregates) ------------------------------------------------------
    # "How many generations / tokens / credits for this user over this period" is answered by a
    # view, not by scanning the table in code. Swappable for MATERIALIZED views without any API
    # change (TD-004). Not tracked by Alembic autogenerate (reflection lists tables only).
    op.execute(
        """
        CREATE VIEW v_generations_daily AS
        SELECT
            date_trunc('day', created_at) AS day,
            kind, provider, model, status,
            count(*)                       AS generations,
            sum(total_tokens)              AS total_tokens,
            sum(units)                     AS units,
            sum(credits_charged)           AS credits_charged,
            avg(latency_ms)::int           AS avg_latency_ms
        FROM generations
        GROUP BY 1, 2, 3, 4, 5
        """
    )
    op.execute(
        """
        CREATE VIEW v_generations_user_totals AS
        SELECT
            user_id, kind,
            count(*)                                          AS generations,
            count(*) FILTER (WHERE status = 'succeeded')      AS succeeded,
            count(*) FILTER (WHERE status = 'failed')         AS failed,
            sum(total_tokens)                                 AS total_tokens,
            sum(units)                                        AS units,
            sum(credits_charged)                              AS credits_charged,
            max(created_at)                                   AS last_generation_at
        FROM generations
        GROUP BY 1, 2
        """
    )


def downgrade() -> None:
    """Drop the whole core schema. For the reversibility test only — NEVER run in prod."""
    op.execute("DROP VIEW IF EXISTS v_generations_user_totals")
    op.execute("DROP VIEW IF EXISTS v_generations_daily")
    for table in (
        "audit_logs",
        "user_profiles",
        "payments",
        "generations",
        "ledger_transactions",
        "wallets",
        "subscriptions",
        "auth_identities",
        "auth_refresh_tokens",
        "auth_devices",
        "users",
    ):
        op.drop_table(table)
    for name, _values in reversed(_ENUMS):
        op.execute(f"DROP TYPE IF EXISTS {name}")
