"""Outgoing checkout — the payment link.

What this fixes: the client used to build the payment link itself, passing a client-controlled
``user_id`` and carrying the app token IN THE BINARY. Two consequences, both real:

1. the callback could not find the user → **lost payments**;
2. the aggregator token was extractable from the app.

Now ``userId`` comes from the verified JWT ``sub`` and travels to the aggregator from the server →
the callback is GUARANTEED to find the user. The secrets stay on the server.

The request body carries ``productId`` (allowlisted against ``PRODUCTS``) and ``customerEmail``
only — no ``userId``, no ``amount``, no ``credits``. ``customerEmail`` is PII: forwarded to the
aggregator, never logged, never persisted. Any upstream failure becomes a plain ``502`` that leaks
neither our token nor the upstream body.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.billing.outcome import (
    CHANNEL_CLOUDPAYMENTS,
    OP_CHECKOUT,
    RESULT_APPLIED,
    RESULT_ERROR,
    RESULT_REJECTED,
    as_reason,
    emit_billing_outcome,
)
from app.config import CoreSettings
from app.errors import (
    CloudPaymentsCheckoutNotConfiguredError,
    ProductNotInChannelError,
    UnknownProductError,
    UpstreamError,
)

logger = logging.getLogger("app.billing_cloudpayments.checkout")

_CHECKOUT_TIMEOUT_SECONDS = 15.0
_OUTCOME_EVENT = "cloudpayments_checkout_outcome"


@dataclass(frozen=True)
class CheckoutResult:
    payment_id: str
    payment_url: str
    status: str
    expires_at: str | None


class CloudPaymentsCheckoutClient:
    """Passthrough to the aggregator's payment-link API. No DB, no persisted state."""

    def __init__(self, settings: CoreSettings) -> None:
        self._settings = settings

    def validate(self, product_id: str) -> None:
        """Allowlist gate, symmetric with the webhook: only issue a link for a product this channel
        could actually credit later. A checkout refusal costs nothing (no payment exists yet) —
        which is exactly why its impact is ``none`` and it raises no alert."""
        from app.products import get_products

        product = get_products().get(product_id)
        if product is None:
            self._emit(RESULT_REJECTED, "unknown_product", product_id=product_id)
            raise UnknownProductError("unknown product")
        if CHANNEL_CLOUDPAYMENTS not in product.channels:
            self._emit(RESULT_REJECTED, "product_not_in_channel", product_id=product_id)
            raise ProductNotInChannelError("product is not sold through this channel")

    def require_configured(self) -> None:
        if not self._settings.cloudpayments_checkout_configured():
            self._emit(RESULT_REJECTED, "not_configured")
            raise CloudPaymentsCheckoutNotConfiguredError("cloudpayments checkout not configured")

    async def create_payment_link(
        self, *, user_id: uuid.UUID, product_id: str, customer_email: str
    ) -> CheckoutResult:
        settings = self._settings
        url = f"{settings.cloudpayments_api_base}/payments/link"
        # multipart/form-data via files= — httpx sets the boundary itself.
        files: dict[str, tuple[None, str]] = {
            "app_id": (None, settings.cloudpayments_app_id),  # server-side, never in the client
            "product_id": (None, product_id),
            "user_id": (None, str(user_id)),  # ⚠ from the JWT sub, NEVER from the body
            "customer_email": (None, customer_email),  # PII: forwarded, never logged/persisted
        }
        headers = {
            "Authorization": f"Bearer {settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=_CHECKOUT_TIMEOUT_SECONDS) as client:
                response = await client.post(url, files=files, headers=headers)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise self._upstream(user_id, product_id) from exc

        if not (200 <= response.status_code < 300):
            raise self._upstream(user_id, product_id)
        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._upstream(user_id, product_id) from exc

        result = self._to_result(body)
        if result is None:
            raise self._upstream(user_id, product_id)

        self._emit(
            RESULT_APPLIED,
            "created",
            user_id=user_id,
            product_id=product_id,
            status=result.status,
            payment_id=result.payment_id,
        )
        return result

    @staticmethod
    def _to_result(body: Any) -> CheckoutResult | None:
        if not isinstance(body, dict):
            return None
        payment_url = body.get("payment_url")
        if not isinstance(payment_url, str) or not payment_url:
            return None
        payment_id = body.get("payment_id")
        status = body.get("status")
        expires_at = body.get("expires_at")
        return CheckoutResult(
            payment_id=str(payment_id) if payment_id is not None else "",
            payment_url=payment_url,
            status=str(status) if status is not None else "",
            expires_at=expires_at if isinstance(expires_at, str) else None,
        )

    def _upstream(self, user_id: uuid.UUID, product_id: str) -> UpstreamError:
        """Generic 502 — the upstream status/body and our token never travel outward."""
        self._emit(RESULT_ERROR, "upstream_error", user_id=user_id, product_id=product_id)
        return UpstreamError("payment provider unavailable")

    @staticmethod
    def _emit(
        result: str,
        reason: str | None,
        *,
        user_id: uuid.UUID | None = None,
        product_id: str | None = None,
        status: str | None = None,
        payment_id: str | None = None,
    ) -> None:
        """One outcome per exit path. Allowlist: customerEmail / token / app_id never appear."""
        emit_billing_outcome(
            event=_OUTCOME_EVENT,
            channel=CHANNEL_CLOUDPAYMENTS,
            op=OP_CHECKOUT,
            result=result,
            reason=as_reason(reason),
            userId=str(user_id) if user_id else None,
            productId=product_id,
            status=status,
            paymentId=payment_id,
        )
