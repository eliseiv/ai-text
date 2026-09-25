"""Core ORM models.

``migrations/env.py`` imports ``Base`` from here, so every model MUST be re-exported: a table
missing from ``Base.metadata`` would be dropped by ``alembic revision --autogenerate``.
A domain adds its own models in ``app/domain/models.py`` (bound to the same ``Base``).
"""

from __future__ import annotations

from app.models.base import Base
from app.models.tables import (
    GENERATION_BILLING_KIND,
    GENERATION_STATUS,
    LEDGER_TX_TYPE,
    PAYMENT_CHANNEL,
    PAYMENT_KIND,
    PAYMENT_STATUS,
    SUBSCRIPTION_STATUS,
    AuditLog,
    AuthDevice,
    AuthIdentity,
    AuthRefreshToken,
    Generation,
    LedgerTransaction,
    Payment,
    Subscription,
    User,
    UserProfile,
    Wallet,
)

__all__ = [
    "GENERATION_BILLING_KIND",
    "GENERATION_STATUS",
    "LEDGER_TX_TYPE",
    "PAYMENT_CHANNEL",
    "PAYMENT_KIND",
    "PAYMENT_STATUS",
    "SUBSCRIPTION_STATUS",
    "AuditLog",
    "AuthDevice",
    "AuthIdentity",
    "AuthRefreshToken",
    "Base",
    "Generation",
    "LedgerTransaction",
    "Payment",
    "Subscription",
    "User",
    "UserProfile",
    "Wallet",
]
