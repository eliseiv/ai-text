"""Observational (NON-BLOCKING) auth of the PUBLIC CloudPayments webhook.

Diagnosis from a real diagnostic log: **the aggregator sends the callback with no authorization at
all** (``authScheme=none``, no signature). Requiring a token therefore means a permanent ``401`` —
and every RU payment lost. Trusting the unsigned body means anyone can credit themselves.

Resolution: the endpoint is **public**, and the trust anchor moves OUT of the callback —
we verify the payment through the aggregator's API with OUR key. This dependency therefore **never
raises**. It only records what the request looked like (scheme WORD and header NAMES — never
values), so that if the aggregator ever starts signing callbacks we will see it immediately.

The generalized template rule: *never design a webhook's authorization from the aggregator's docs —
only from the REAL request. If a channel does not sign its callbacks, the callback is only a
trigger.*
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.api_gateway.openapi_security import cloudpayments_webhook_scheme
from app.config import get_settings
from app.observability.logging import log_event

logger = logging.getLogger("app.billing_cloudpayments.auth")

# Header NAMES only (never values) — lets us notice if the aggregator starts sending a signature.
_AUTH_HEADER_ALLOWLIST = (
    "authorization",
    "x-api-key",
    "x-signature",
    "x-sign",
    "x-webhook-signature",
    "x-content-hmac",
    "content-hmac",
    "signature",
)


def _extract_credential(authorization: str | None) -> str | None:
    """Lenient extraction: a strict ``Bearer <token>`` parser produced 401s on a VALID
    secret, because the aggregator did not use that shape."""
    if authorization is None:
        return None
    value = authorization.strip()
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
        return parts[1].strip() or None
    return value


def _auth_scheme_label(authorization: str | None) -> str:
    """The scheme WORD only — never the token value."""
    if authorization is None:
        return "none"
    value = authorization.strip()
    if not value:
        return "empty"
    parts = value.split(None, 1)
    return parts[0].lower() if len(parts) == 2 else "raw"


def require_cloudpayments_webhook(
    request: Request,
    _scheme: Annotated[
        HTTPAuthorizationCredentials | None, Depends(cloudpayments_webhook_scheme)
    ] = None,
) -> None:
    """NEVER raises. Records one observational record; the real trust anchor is the payment verify.

    ``matched`` is computed only against the LEGACY ``CLOUDPAYMENTS_WEBHOOK_TOKEN`` when that is
    configured, and it gates nothing — it exists purely so the log can tell us whether the
    aggregator ever starts presenting the token it never presents.
    """
    header = request.headers.get("authorization")
    legacy_secret = get_settings().cloudpayments_webhook_token
    matched = (
        hmac.compare_digest(_extract_credential(header) or "", legacy_secret)
        if legacy_secret
        else False
    )
    log_event(
        logger,
        logging.INFO,
        "cloudpayments_webhook_auth_observed",
        matched=matched,
        authScheme=_auth_scheme_label(header),
        presentAuthHeaders=[n for n in _AUTH_HEADER_ALLOWLIST if n in request.headers],
    )
