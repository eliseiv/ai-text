"""ORM definitions of the CORE schema — 11 tables.

INVARIANT: every core table has a model HERE. In the source service
``auth_devices`` / ``auth_refresh_tokens`` lived only in a migration + raw SQL while
``migrations/env.py`` pointed at ``Base.metadata`` — so ``alembic --autogenerate`` would have
emitted ``drop_table('auth_devices')`` (i.e. dropped the deviceId→userId mapping the payment
webhooks resolve users with). Fixed here, and locked by the ``compare_metadata()`` test.

Money/credits are integral (BIGINT). All timestamps are ``timestamptz`` (UTC). The DB-level
CHECK constraints are the money invariants themselves (BR-7 / "no money outside the ledger"),
not decoration: they hold even when application code has a bug.

The two views (``v_generations_daily``, ``v_generations_user_totals``) are created by raw SQL in
the baseline migration; Alembic does not track views.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import BIGINT, ENUM, INTEGER, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# --- Enum value tuples (mirror CREATE TYPE) ---
SUBSCRIPTION_STATUS = ("active", "expired", "none")
LEDGER_TX_TYPE = ("credit", "debit")
# generation lifecycle. `pending` + `provider_ref` + poll() exist from day one so the
# async path can be added later without a migration.
GENERATION_STATUS = ("pending", "running", "succeeded", "failed", "canceled")
# how the generation was paid for. `unbilled` = the service worked for free (quote()
# under-estimated and the debit found no credits); the user never goes negative.
GENERATION_BILLING_KIND = ("none", "credits", "trial", "unbilled")
# unified payments journal (replaces the two per-channel webhook tables).
PAYMENT_CHANNEL = ("apple_storekit", "adapty", "cloudpayments")
# `refund` (migration 0003): a store/aggregator refund of an earlier purchase.
PAYMENT_KIND = ("subscription", "tokens", "subscription_event", "refund")
# `received` is the NEUTRAL start status written by layer 1 — never an optimistic
# `granted`: a row committed without a grant must be visible as unfinished, not as a success.
# `refunded` (migration 0003): a refund row; credits taken back are a ledger debit.
PAYMENT_STATUS = ("received", "granted", "replayed", "no_grant", "rejected", "refunded")

_subscription_status_enum = ENUM(
    *SUBSCRIPTION_STATUS, name="subscription_status", create_type=False
)
_ledger_tx_type_enum = ENUM(*LEDGER_TX_TYPE, name="ledger_tx_type", create_type=False)
_generation_status_enum = ENUM(*GENERATION_STATUS, name="generation_status", create_type=False)
_generation_billing_kind_enum = ENUM(
    *GENERATION_BILLING_KIND, name="generation_billing_kind", create_type=False
)
_payment_channel_enum = ENUM(*PAYMENT_CHANNEL, name="payment_channel", create_type=False)
_payment_kind_enum = ENUM(*PAYMENT_KIND, name="payment_kind", create_type=False)
_payment_status_enum = ENUM(*PAYMENT_STATUS, name="payment_status", create_type=False)

_uuid_default = sa_text("gen_random_uuid()")
_now = sa_text("now()")


class User(Base):
    """Identity anchor: ``users.id`` == JWT ``sub`` (lazy provisioning)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    # BR-1: lifetime trial flag. Flipped exactly once by an atomic
    # `UPDATE users SET trial_used = TRUE WHERE id = :id AND trial_used = FALSE` (race-free).
    trial_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )


class AuthDevice(Base):
    """deviceId → userId mapping. One device = one identity (``device_id`` is the PK).

    CRITICAL FOR BILLING: this is the only trusted source of the deviceId→userId link when a
    payment webhook resolves the user — payment providers send the DEVICE id
    in their "user identifier" field, not our ``userId``.
    """

    __tablename__ = "auth_devices"

    device_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        Index("ix_auth_devices_user", "user_id"),
        # Functional index backing the case-insensitive resolve `WHERE lower(device_id) = :x`
        # (invariant 9). A plain PK index cannot serve that
        # predicate → without this the webhook resolve degrades to a sequential scan.
        # Deliberately NOT unique (two devices may differ only by casing).
        Index("ix_auth_devices_lower_device_id", sa_text("lower(device_id)")),
    )


class AuthRefreshToken(Base):
    """Opaque refresh tokens stored ONLY as sha256 (``token_hash``).

    Valid ⟺ ``used_at IS NULL AND revoked_at IS NULL AND expires_at > now()``. Single-use
    rotation + reuse detection revoke the whole device chain. Cleanup of expired rows: TD-002.
    """

    __tablename__ = "auth_refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("auth_devices.device_id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        Index("ux_refresh_token_hash", "token_hash", unique=True),
        Index("ix_refresh_user_device", "user_id", "device_id"),
    )


class AuthIdentity(Base):
    """External identity-provider link (Sign in with Apple on start).

    ``UNIQUE(provider, subject)`` gives cross-device resolution (one Apple account = one userId)
    and race safety (``ON CONFLICT (provider, subject) DO NOTHING`` + re-read). The Apple
    identity token itself is verified but never stored — only ``subject`` / ``email`` land here.
    """

    __tablename__ = "auth_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # 'apple' (extensible)
    subject: Mapped[str] = mapped_column(Text, nullable=False)  # provider-stable id (apple sub)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)  # optional (private-relay)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        Index("ux_auth_identities_provider_subject", "provider", "subject", unique=True),
        Index("ix_auth_identities_user", "user_id"),
    )


class Subscription(Base):
    """Subscription state. Lazy expiry: ``active`` + ``expires_at <= now()`` reads as expired."""

    __tablename__ = "subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(
        _subscription_status_enum, nullable=False, server_default=sa_text("'none'")
    )
    plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (Index("ix_subscriptions_expires_at", "expires_at"),)


class Wallet(Base):
    """Credit balance. ``ck_wallets_balance_nonneg`` makes a negative balance impossible (AC-3)."""

    __tablename__ = "wallets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=sa_text("0"))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (CheckConstraint("balance >= 0", name="ck_wallets_balance_nonneg"),)


class LedgerTransaction(Base):
    """Money journal. ``ux_ledger_idempotency`` is THE single idempotency point of money.

    Key namespaces are fixed (``generation:{id}``, ``adapty-txn:{txn}``,
    ``cp-txn:{payment_id}``, ``sub-grant:{txn}``, ``token-purchase:{txn}``, ``admin-grant:{key}``,
    ``admin-sub-grant:{key}``). Redis marks / webhook delivery dedup are OTHER layers, never a
    replacement for this unique index.
    """

    __tablename__ = "ledger_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(_ledger_tx_type_enum, nullable=False)
    amount: Mapped[int] = mapped_column(BIGINT, nullable=False)
    # 'generation' | 'subscription_grant' | 'token_purchase' | 'admin_grant' | ...
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_ledger_amount_positive"),
        UniqueConstraint("user_id", "idempotency_key", name="ux_ledger_idempotency"),
        Index("ix_ledger_user_created", "user_id", sa_text("created_at DESC")),
    )


class Generation(Base):
    """One row = the provider WAS called. Policy blocks create no row.

    The CHECK constraints are the money invariants at DB level:
      * ``ck_generations_charge_only_succeeded`` — BR-7: a failed generation cannot be charged,
        even if application code tries to;
      * ``ck_generations_ledger_link`` — charged credits MUST have a ledger row (no money
        outside the journal);
      * ``ck_generations_completed_at`` — terminal status ⟺ ``completed_at`` is set;
      * ``ck_generations_error_code`` — only a failed generation carries an ``error_code``.
    """

    __tablename__ = "generations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # 'image' | 'text' | 'video' | ...
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        _generation_status_enum, nullable=False, server_default=sa_text("'running'")
    )
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # usage — tokens (LLM-like domains)
    input_tokens: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=sa_text("0"))
    output_tokens: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=sa_text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(
        BIGINT, nullable=False, server_default=sa_text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        BIGINT, nullable=False, server_default=sa_text("0")
    )
    total_tokens: Mapped[int] = mapped_column(
        BIGINT,
        Computed("input_tokens + output_tokens", persisted=True),
        nullable=False,
    )
    # usage — units (everything else): images, seconds, pages, calls
    units: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=sa_text("0"))
    unit_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'call'"))

    credits_charged: Mapped[int] = mapped_column(
        BIGINT, nullable=False, server_default=sa_text("0")
    )
    billing_kind: Mapped[str] = mapped_column(
        _generation_billing_kind_enum, nullable=False, server_default=sa_text("'none'")
    )
    ledger_tx_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )

    latency_ms: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    attempt: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=sa_text("1"))
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    # upstream job id — the entry point of a future async callback (ux_generations_provider_ref)
    provider_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # redacted, <= 1 KB
    # provider `output` — ONLY when smaller than GENERATION_META_MAX_BYTES. The core never
    # stores blobs: heavy artifacts go to the domain's own storage, a link comes back here.
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="ux_generations_idempotency"),
        CheckConstraint(
            "credits_charged = 0 OR status = 'succeeded'",
            name="ck_generations_charge_only_succeeded",
        ),
        CheckConstraint(
            "credits_charged = 0 OR ledger_tx_id IS NOT NULL",
            name="ck_generations_ledger_link",
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed','canceled')) = (completed_at IS NOT NULL)",
            name="ck_generations_completed_at",
        ),
        CheckConstraint(
            "status = 'failed' OR error_code IS NULL",
            name="ck_generations_error_code",
        ),
        Index("ix_generations_user_created", "user_id", sa_text("created_at DESC")),
        Index("ix_generations_kind_created", "kind", sa_text("created_at DESC")),
        # partial — cheap inflight lookup (guard step 1.5 + future stuck-generation reaper).
        Index(
            "ix_generations_inflight",
            "status",
            "created_at",
            postgresql_where=sa_text("status IN ('pending','running')"),
        ),
        # partial UNIQUE — an async callback cannot apply to two rows.
        Index(
            "ux_generations_provider_ref",
            "provider",
            "provider_ref",
            unique=True,
            postgresql_where=sa_text("provider_ref IS NOT NULL"),
        ),
        Index("brin_generations_created", "created_at", postgresql_using="brin"),
    )


class Payment(Base):
    """Unified journal of ALL payments — StoreKit / Adapty / CloudPayments.

    LAYER 1 (delivery dedup): ``ux_payments_channel_external`` — a repeat of the SAME webhook.
    LAYER 2 (grant idempotency): ``ledger_transactions.ux_ledger_idempotency`` — several
    DIFFERENT events of one billing period. **Different columns on purpose**: merging them
    silently doubles grants (Adapty sends trial_started + subscription_started with different
    ``profile_event_id`` but ONE ``transaction_id``).

    ``amount`` / ``currency`` are INFORMATIONAL (reconciliation with the provider's statement)
    and NEVER the source of credits — credits come only from the server-side ``PRODUCTS`` map
    (BR-8). ``payload`` is a SANITIZED per-channel projection (no card PII, no bearer).
    """

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(_payment_channel_enum, nullable=False)
    # StoreKit transactionId | Adapty profile_event_id | broadapps payment_id (from verify).
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # key of PRODUCTS
    kind: Mapped[str] = mapped_column(_payment_kind_enum, nullable=False)
    status: Mapped[str] = mapped_column(
        _payment_status_enum, nullable=False, server_default=sa_text("'received'")
    )
    credits_granted: Mapped[int] = mapped_column(
        BIGINT, nullable=False, server_default=sa_text("0")
    )
    amount: Mapped[decimal.Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    grant_idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    ledger_tx_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    payload_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=sa_text("1")
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    processed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        UniqueConstraint("channel", "external_id", name="ux_payments_channel_external"),
        CheckConstraint(
            "credits_granted = 0 OR ledger_tx_id IS NOT NULL",
            name="ck_payments_grant_link",
        ),
        CheckConstraint(
            "kind = 'subscription_event' OR product_id IS NOT NULL",
            name="ck_payments_product",
        ),
        CheckConstraint(
            "status <> 'no_grant' OR credits_granted = 0",
            name="ck_payments_no_grant",
        ),
        Index("ix_payments_user_received", "user_id", sa_text("received_at DESC")),
        Index("ix_payments_channel_received", "channel", sa_text("received_at DESC")),
        Index(
            "ix_payments_product",
            "product_id",
            postgresql_where=sa_text("product_id IS NOT NULL"),
        ),
    )


class UserProfile(Base):
    """Profile fields kept OUT of ``users`` so the domain may extend ``users`` freely.

    Created lazily (upsert on the first ``PATCH /v1/profile``); absent → defaults are returned.
    The human-readable ``accountId`` is NOT stored — it is derived from ``user_id`` on the fly.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )


class AuditLog(Base):
    """Append-only audit journal (application-level; DB-level ban of revisions is TD-001).

    ``generation_id`` references a CORE table (``generations``). The source had
    ``session_id → chat_sessions`` — an FK from the core onto a DOMAIN table, which made the core
    depend on the domain. There is no domain FK here.
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("generations.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        Index("ix_audit_user_created", "user_id", sa_text("created_at DESC")),
        Index("ix_audit_event_type", "event_type", sa_text("created_at DESC")),
    )
