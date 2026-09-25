"""payments: refunds — payment_status 'refunded', payment_kind 'refund'

A refund is journalled as its OWN ``payments`` row (kind ``refund``, status ``refunded``) next to
the original grant, which stays ``granted``: the journal is append-only history, and "was granted,
later refunded" must stay readable. The credits taken back live in ``ledger_transactions``
(a ``debit`` with key ``refund:<original grant key>``).

``ALTER TYPE … ADD VALUE`` cannot be undone in PostgreSQL without recreating the type, so
``downgrade()`` is a no-op; the baseline downgrade drops the types anyway.

Revision ID: 0003_payment_refunds
Revises: 0002_sources_domain
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_payment_refunds"
down_revision: str | None = "0002_sources_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE payment_status ADD VALUE IF NOT EXISTS 'refunded'")
    op.execute("ALTER TYPE payment_kind ADD VALUE IF NOT EXISTS 'refund'")


def downgrade() -> None:
    # Enum values cannot be dropped in place; harmless to keep (nothing references them).
    pass
