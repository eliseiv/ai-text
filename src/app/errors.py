"""Technical errors mapped to HTTP codes.

A BUSINESS block is NOT an error: it returns ``200 {status: "blocked", blockReason}``.
These exceptions cover only technical failures (4xx/5xx).

The wire ``code`` (what the client machine-reads) is the contract — the error handler serializes
``exc.code``, not ``exc.message``. A new machine-readable code therefore needs its OWN subclass,
not a re-used base class with a different message.
"""

from __future__ import annotations


class AppError(Exception):
    """Base technical error. ``code`` is the machine-readable value of the contract."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.code
        super().__init__(self.message)


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class SubscriptionRequiredError(ForbiddenError):
    """Token purchase attempted without an active subscription.

    403 ``subscription_required``: a top-up is not a generation, so the "blocked = 200" rule does
    not apply here.
    """

    code = "subscription_required"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class UserNotFoundError(NotFoundError):
    """The userId targeted by an admin op does not exist; admin never creates users."""

    code = "user_not_found"


class GenerationNotFoundError(NotFoundError):
    """``GET /v1/generations/{id}`` — foreign or missing generation.

    404 ``generation_not_found``: a foreign generation is never distinguishable from a missing
    one (isolation — we do not reveal that someone else's row exists).
    """

    code = "generation_not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InsufficientCreditsError(ConflictError):
    """Balance dropped below the required amount after the policy allowed."""

    code = "insufficient_credits"


class AlreadyInProgressError(ConflictError):
    """A generation with the SAME ``Idempotency-Key`` is already running (anchor conflict).

    409 ``already_in_progress``. The result WILL exist — the client waits and fetches it via
    ``GET /v1/generations/{id}``. Distinct code from ``too_many_inflight``, where the request was
    refused outright and no row was created: different cause, different client reaction.
    """

    code = "already_in_progress"


class TooManyInflightError(ConflictError):
    """The user already has ``GENERATION_MAX_INFLIGHT_PER_USER`` pending/running generations.

    409 ``too_many_inflight`` (guard step 1.5) — do NOT confuse with
    ``409 already_in_progress`` (the SAME ``Idempotency-Key`` is still running). Different cause,
    different client action: here the user must wait for any of their generations to finish.
    """

    code = "too_many_inflight"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_error"


class InvalidTransactionError(ValidationFailedError):
    """A StoreKit transaction failed cryptographic verification (signature / chain / bundle).

    422 ``invalid_transaction``. A forged transaction and a legitimate-but-failing one are
    indistinguishable here — which is why a SPIKE of these is an alert (an expired Apple root CA
    means paying users are getting nothing), not routine noise.
    """

    code = "invalid_transaction"


class VerificationUnavailableError(ValidationFailedError):
    """No Apple root CA is mounted → the transaction CANNOT be verified.

    422 ``verification_unavailable``, fail-closed. Accepting an unverifiable transaction would turn
    the subscription into a field the client fills in himself: anyone could activate a subscription
    with one request. "Cannot verify" therefore means REFUSE, never "trust the client".
    """

    code = "verification_unavailable"


class UnknownProductError(ValidationFailedError):
    """The (verified) productId is not in the server-side ``PRODUCTS`` map — fail-closed (BR-8).

    422 ``unknown_product``. There is NO fallback grant: crediting a default amount for a product
    nobody configured is the same anti-tamper violation as taking the amount from the client.
    NOTE: on the StoreKit paths this fires AFTER verification ⇒ the user has already paid Apple ⇒
    the outcome is classified ``lost_payment`` and must be topped up by hand once PRODUCTS is fixed.
    """

    code = "unknown_product"


class ProductNotInChannelError(ValidationFailedError):
    """The product exists but is not allowed in this payment channel (``channels`` allowlist).

    422 ``product_not_in_channel``. Closes the hole the source had: a CloudPayments callback naming
    an Apple productId would otherwise be credited at the Apple tier.
    """

    code = "product_not_in_channel"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"


class ServiceUnavailableError(AppError):
    """A required dependency/feature is not configured (e.g. the auth issuer has no key)."""

    status_code = 503
    code = "service_unavailable"


class ProviderNotConfiguredError(ServiceUnavailableError):
    """``GENERATION_PROVIDER`` names a provider nobody registered.

    503 ``provider_not_configured``: an operational mis-configuration, not a client error. The
    core NEVER falls back to some "default" provider — a silent fallback would generate with the
    wrong upstream and bill for it.
    """

    code = "provider_not_configured"


class CloudPaymentsWebhookMisconfiguredError(ServiceUnavailableError):
    """The RU webhook cannot verify payments — ``CLOUDPAYMENTS_API_TOKEN`` is unset.

    Overrides ``status_code`` to 500 (not the 503 of the base) so the aggregator treats it as a
    transient server fault and RETRIES until the operator sets the token — a real payment must
    not be dropped because of our mis-configuration.
    """

    status_code = 500
    code = "cloudpayments_webhook_misconfigured"


class CloudPaymentsVerificationUnavailableError(AppError):
    """The broadapps verification GET failed transiently — credit deferred, retriable.

    500 so the whole callback is re-delivered later (idempotency by ``payment_id`` keeps the
    reprocessing safe). A broadapps ``404`` is NOT this error — it means "no payments"
    (permanent) and yields ``no_creditable_payment`` (200).
    """

    status_code = 500
    code = "cloudpayments_verification_unavailable"


class CloudPaymentsCheckoutNotConfiguredError(ServiceUnavailableError):
    """RU checkout is not configured on this instance — 503, distinct code so the
    client can tell "feature absent here" from an aggregator outage (502)."""

    code = "cloudpayments_checkout_not_configured"
