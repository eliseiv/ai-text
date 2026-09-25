"""Payment verification through the aggregator's API — THE trust anchor of the RU channel.

The callback carries no signature and no auth, so it is only a TRIGGER. The single trusted "money
happened" signal is OUR OWN request to the aggregator, with OUR key.

Consequently a forged callback is harmless: at most it triggers a useless GET, verify confirms
nothing, and the outcome is ``no_creditable_payment`` — **zero credits**.

**A transient verify failure must be a 500, not a 200.** ``200`` means "accepted" to the aggregator
→ it never re-delivers → the payment is lost FOREVER. ``500`` makes it re-deliver, and the payment
simply waits for us to come back.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import CoreSettings
from app.errors import CloudPaymentsVerificationUnavailableError
from app.observability.logging import log_event
from app.observability.metrics import cloudpayments_verify_errors_total

logger = logging.getLogger("app.billing_cloudpayments.verify")

_VERIFY_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CreditablePayment:
    """A payment the aggregator CONFIRMED. ``payment_id`` (from verify!) keys both dedup layers."""

    payment_id: str
    product_code: str
    payment_type: str  # authoritative class from the aggregator
    status: str
    paid_at: datetime.datetime


class CloudPaymentsVerifyClient:
    """Stateless client of ``GET {api_base}/users/{deviceId}/payments``."""

    def __init__(self, settings: CoreSettings) -> None:
        self._settings = settings

    async def list_payments(self, *, device_id: str) -> list[dict[str, Any]]:
        """Fetch the device's payments. ``404`` = "no payments" (permanent) → ``[]``, NOT a retry.

        ``device_id`` is validated as a UUID by the caller before it reaches this path (anti-SSRF);
        the host comes from config only, never from the callback body. Our bearer and the upstream
        body are never logged or proxied outward.
        """
        settings = self._settings
        url = f"{settings.cloudpayments_api_base}/users/{device_id}/payments"
        headers = {
            "Authorization": f"Bearer {settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT_SECONDS) as client:
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise self._unavailable("timeout") from exc
        except httpx.RequestError as exc:
            raise self._unavailable("timeout") from exc

        if response.status_code == 404:
            return []
        if not (200 <= response.status_code < 300):
            raise self._unavailable("non_2xx")

        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._unavailable("malformed") from exc
        if not isinstance(body, dict):
            raise self._unavailable("malformed")
        data = body.get("data")
        if not isinstance(data, list):
            raise self._unavailable("malformed")
        return [item for item in data if isinstance(item, dict)]

    def _unavailable(self, reason: str) -> CloudPaymentsVerificationUnavailableError:
        """Each sample of this metric == one retriable 500 == one payment waiting, not lost."""
        cloudpayments_verify_errors_total.labels(reason=reason).inc()
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_verify_outcome",
            verify="api_error",
            reason=reason,
        )
        return CloudPaymentsVerificationUnavailableError("cloudpayments verification unavailable")


def _parse_paid_at(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def payment_statuses(data: list[dict[str, Any]]) -> list[str]:
    """The raw ``status`` values, for the outcome log — safe (not PII, not a secret) and used to
    calibrate ``CLOUDPAYMENTS_PAID_STATUSES`` against real data."""
    return [str(item.get("status")) for item in data if isinstance(item, dict)]


@dataclass(frozen=True)
class RefundedPayment:
    """A payment the aggregator reports as REFUNDED (confirmed by OUR verify call)."""

    payment_id: str
    product_code: str | None
    payment_type: str | None


def select_refunded_payments(
    data: list[dict[str, Any]], *, refunded_statuses: frozenset[str]
) -> list[RefundedPayment]:
    """PURE: payments in a refunded status. No freshness window — a refund may come weeks after
    the payment; the caller only acts on payments WE credited, so history cannot be replayed."""
    refunded: list[RefundedPayment] = []
    for item in data:
        status = str(item.get("status") or "").strip().lower()
        if status not in refunded_statuses:
            continue
        payment_id = item.get("payment_id")
        if not isinstance(payment_id, str) or not payment_id.strip():
            continue
        raw_product = item.get("product")
        product: dict[str, Any] = raw_product if isinstance(raw_product, dict) else {}
        code = product.get("code") if isinstance(product.get("code"), str) else None
        ptype = product.get("payment_type")
        refunded.append(
            RefundedPayment(
                payment_id=payment_id.strip(),
                product_code=code.strip() if code else None,
                payment_type=ptype.strip().lower() if isinstance(ptype, str) else None,
            )
        )
    return refunded


def select_creditable_payments(
    data: list[dict[str, Any]],
    *,
    paid_statuses: frozenset[str],
    now: datetime.datetime,
    freshness_hours: int,
) -> list[CreditablePayment]:
    """PURE reconciliation: which verified payments may be credited.

    Kept iff ALL hold: status ∈ paid set; ``paid_at`` within the freshness window (otherwise the
    FIRST callback for a user with history would credit his entire back-catalogue at once); and the
    payment carries a usable ``payment_id`` / ``product.code`` / ``product.payment_type``.
    """
    cutoff = now - datetime.timedelta(hours=freshness_hours)
    creditable: list[CreditablePayment] = []
    for item in data:
        status = str(item.get("status") or "").strip().lower()
        if status not in paid_statuses:
            continue
        paid_at = _parse_paid_at(item.get("paid_at"))
        if paid_at is None or paid_at < cutoff:
            continue
        payment_id = item.get("payment_id")
        if not isinstance(payment_id, str) or not payment_id.strip():
            continue
        product = item.get("product")
        if not isinstance(product, dict):
            continue
        product_code = product.get("code")
        payment_type = product.get("payment_type")
        if not isinstance(product_code, str) or not product_code.strip():
            continue
        if not isinstance(payment_type, str) or not payment_type.strip():
            continue
        creditable.append(
            CreditablePayment(
                payment_id=payment_id.strip(),
                product_code=product_code.strip(),
                payment_type=payment_type.strip().lower(),
                status=status,
                paid_at=paid_at,
            )
        )
    return creditable
