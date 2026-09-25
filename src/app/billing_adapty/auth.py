"""Isolated bearer authorization of the Adapty webhook.

A per-route dependency, not middleware: the endpoint is fully separated from the user-JWT chain and
from the admin token, and uses its own per-instance secret.

* secret not configured → **500** (mis-configuration; Adapty retries until the operator sets it —
  a blank secret never matches anything);
* missing / mismatching token → **401**, without saying which.

Comparison is constant-time. The secret is never logged (``authorization`` is in the redaction
denylist).
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials

from app.api_gateway.openapi_security import adapty_webhook_scheme
from app.config import get_settings
from app.errors import ServiceUnavailableError, UnauthorizedError


class AdaptyWebhookMisconfiguredError(ServiceUnavailableError):
    """``ADAPTY_WEBHOOK_SECRET`` is unset — the endpoint cannot authenticate anything.

    Overrides the base 503 with **500** per the contract: Adapty must treat it as a transient
    server fault and KEEP RETRYING, so a real subscription event is not dropped because we forgot
    to configure the secret.
    """

    status_code = 500
    code = "adapty_webhook_misconfigured"


def require_adapty_webhook(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(adapty_webhook_scheme)
    ] = None,
) -> None:
    secret = get_settings().adapty_webhook_secret
    if not secret:
        raise AdaptyWebhookMisconfiguredError("adapty webhook secret is not configured")
    presented = credentials.credentials if credentials is not None else None
    if presented is None or not hmac.compare_digest(presented, secret):
        raise UnauthorizedError("invalid adapty webhook token")
