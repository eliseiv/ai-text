"""Correlation context vars for structured logs/traces.

``requestId`` is a per-HTTP-request correlation id (``X-Request-Id``). It is NOT the billing
idempotency key (that is ``Idempotency-Key`` → ``ledger_transactions.idempotency_key``).
"""

from __future__ import annotations

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
generation_id_var: ContextVar[str | None] = ContextVar("generation_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


def set_request_id(value: str | None) -> None:
    request_id_var.set(value)


def get_request_id() -> str | None:
    return request_id_var.get()


def set_generation_id(value: str | None) -> None:
    generation_id_var.set(value)


def set_user_id(value: str | None) -> None:
    user_id_var.set(value)
