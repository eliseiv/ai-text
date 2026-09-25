"""TRUTH OF CONTENT: every declared ``impact`` is RECOMPUTED from facts observed in a real run.

This is the test the whole observability rule-set exists for (R-OBS-3/4/6). It does NOT read the
inputs from a table in ``docs/`` — that would only prove the table agrees with itself. For each
exit path it RUNS the real code and derives:

| input | how it is derived here — a FACT, not a document |
|---|---|
| ``money`` | the CALL COUNTER of the verifier / the respx verify route: did a verifying step |
| | actually complete BEFORE this branch? (plus the positional facts: is the channel an |
| | authenticated M2M one, and was the body recognised as a granting event) |
| ``credited`` | ``SELECT`` from ``ledger_transactions`` AFTER processing — the PAYER's state, |
| | not what this branch happened to grant |
| ``deliberate`` | the branch answered ``403`` — our policy guard is the only thing in billing |
| | that refuses an operation it COULD have completed |
| ``system_broken`` | the test itself broke our side (unset the API token, removed the root CA, |
| | took the upstream down, sent an unparseable body) |
| ``remediation`` | EXECUTED: repair our side, re-process the SAME stored event, look for the |
| | grant |

Then ``reference_impact(...)`` (the total function, transcribed in
``tests/support/impact_reference.py``) is compared with what the CODE declares. A mismatch is a
failure — in either direction (R-OBS-4: an over-estimate kills the alert as surely as an
under-estimate loses the money).
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.outcome import _IMPACT, impact_for
from app.config import get_settings
from app.errors import InvalidTransactionError, VerificationUnavailableError
from tests.conftest import (
    ADAPTY_SECRET,
    CLOUDPAYMENTS_API_BASE,
    PRODUCT_SUB,
    PRODUCT_SUB_APPLE_ONLY,
    PRODUCT_SUB_RU_ONLY,
    PRODUCT_TOKENS,
    PRODUCT_TOKENS_RU_ONLY,
    FakeStoreKitVerifier,
    auth_headers,
    has_ledger_key,
    metric_value,
    seed_user,
)
from tests.support.impact_reference import reference_impact

ADAPTY_WEBHOOK = "/v1/billing/adapty/webhook"
CP_WEBHOOK = "/v1/billing/cloudpayments/webhook"
CP_CHECKOUT = "/v1/billing/cloudpayments/checkout"
ADAPTY_AUTH = {"Authorization": f"Bearer {ADAPTY_SECRET}"}

DEVICE_UPPER = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
DEVICE_LOWER = DEVICE_UPPER.lower()


# ---------------------------------------------------------------------------------------------
# What a run of one exit path lets us OBSERVE
# ---------------------------------------------------------------------------------------------
@dataclass
class Observed:
    http_status: int
    # A verifying step (StoreKit JWS / broadapps GET verify) COMPLETED before this branch.
    verifier_completed: bool = False
    # An external party asserts that money changed hands (a StoreKit transaction was presented, a
    # payment platform delivered an event, the aggregator's callback says Status=Completed).
    external_claims_payment: bool = False
    # The event arrived over an authenticated M2M channel (Adapty's bearer was verified).
    channel_authenticated: bool = False
    # True  = the body was parsed AND recognised as a GRANTING event
    # False = parsed and recognised as NOT a payment (ping / unknown type / declined callback)
    # None  = not parseable at all ⇒ we cannot know whether it was granting
    parsed_granting: bool | None = None
    credited: bool = False
    broke_our_side: bool = False
    remediation: str = "investigate"
    result: str = "ignored"
    channel: str = ""


def derive_money(obs: Observed) -> str:
    """POSITIONAL, three-valued. Never a judgement."""
    if obs.verifier_completed:
        return "confirmed"
    if obs.channel_authenticated and obs.parsed_granting is True:
        # Authentication proves the SENDER; recognising the event as granting is what makes it a
        # payment. Both hold here.
        return "confirmed"
    if obs.parsed_granting is False:
        return "absent"  # recognised, and it is not a payment
    if obs.external_claims_payment:
        return "claimed"  # asserted by an outsider, verified by nobody
    return "absent"


def derive_deliberate(obs: Observed) -> bool:
    """A 403 is, by definition, "we could have completed it and chose not to" — the policy guard.
    Validation (422), brokenness (5xx) and acceptance (2xx) are not deliberate refusals."""
    return obs.http_status == 403


@dataclass
class Scenario:
    op: str
    reason: str
    run: Callable[..., Awaitable[Observed]]
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def adapty_body(
    event_id: str,
    event_type: str = "subscription_started",
    *,
    txn: str | None = "T-imp",
    product: str | None = PRODUCT_SUB,
    customer: str | None = DEVICE_LOWER,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "profile_event_id": event_id,
        "event_type": event_type,
        "event_properties": {"vendor_product_id": product},
    }
    if txn is not None:
        body["event_properties"]["transaction_id"] = txn
    if customer is not None:
        body["customer_user_id"] = customer
    return body


def cp_body(account_id: str = DEVICE_UPPER, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "Status": "Completed",
        "OperationType": "Payment",
        "AccountId": account_id,
        "TransactionId": 1,
    }
    body.update(overrides)
    return body


def cp_payment(
    payment_id: str = "pay-imp",
    product_code: str = PRODUCT_SUB,
    payment_type: str = "subscription",
) -> dict[str, Any]:
    return {
        "payment_id": payment_id,
        "status": "succeeded",
        "paid_at": datetime.datetime.now(tz=datetime.UTC).isoformat(),
        "product": {"code": product_code, "payment_type": payment_type},
    }


def cp_verify_route(payments: list[dict[str, Any]] | None = None) -> respx.Route:
    return respx.get(url__regex=rf"{CLOUDPAYMENTS_API_BASE}/users/.*/payments").mock(
        return_value=httpx.Response(200, json={"data": payments or []})
    )


# ---------------------------------------------------------------------------------------------
# Scenarios — one per (op, reason) row of the matrix
# ---------------------------------------------------------------------------------------------
async def _storekit_reject(
    client: AsyncClient,
    session: AsyncSession,
    storekit: FakeStoreKitVerifier,
    *,
    path: str,
    error: Exception | None = None,
    product: str | None = None,
    subscription: str | None = "active",
) -> Observed:
    user_id = await seed_user(session, subscription=subscription)
    if error is not None:
        storekit.error = error
        # verification_unavailable is reachable ONLY with our deployment broken (no root CA
        # mounted); invalid_transaction is not our fault.
        broke = isinstance(error, VerificationUnavailableError)
    else:
        storekit.script(transaction_id="imp-txn", product_id=product or "x")
        broke = False

    response = await client.post(path, json={"transaction": "jws"}, headers=auth_headers(user_id))
    verified = error is None and storekit.calls > 0
    credited = await has_ledger_key(session, user_id, "sub-grant:imp-txn") or await has_ledger_key(
        session, user_id, "token-purchase:imp-txn"
    )
    return Observed(
        http_status=response.status_code,
        verifier_completed=verified,
        external_claims_payment=True,  # a StoreKit transaction WAS presented
        credited=credited,
        broke_our_side=broke,
        result="rejected",
        channel="apple_storekit",
    )


async def _adapty(
    client: AsyncClient,
    session: AsyncSession,
    *,
    body: Any,
    seed_device: bool = True,
    granting: bool | None = True,
    broke_our_side: bool = False,
    result: str = "ignored",
    grant_key: str = "sub-grant:T-imp",
) -> Observed:
    user_id = await seed_user(session, device_id=DEVICE_UPPER) if seed_device else uuid.uuid4()
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    response = await client.post(ADAPTY_WEBHOOK, content=content, headers=ADAPTY_AUTH)
    observed = Observed(
        http_status=response.status_code,
        external_claims_payment=bool(content),  # an empty body is the platform's ping
        channel_authenticated=True,  # the bearer was verified by the dependency
        parsed_granting=granting,
        credited=await has_ledger_key(session, user_id, grant_key),
        broke_our_side=broke_our_side,
        result=result,
        channel="adapty",
    )
    return observed


# --- StoreKit: subscription_sync -----------------------------------------------------------------
async def sub_sync_verification_unavailable(
    client: AsyncClient, session: AsyncSession, storekit: FakeStoreKitVerifier, **_: Any
) -> Observed:
    return await _storekit_reject(
        client,
        session,
        storekit,
        path="/v1/subscription/sync",
        error=VerificationUnavailableError("no Apple root CA mounted"),
    )


async def sub_sync_invalid_transaction(
    client: AsyncClient, session: AsyncSession, storekit: FakeStoreKitVerifier, **_: Any
) -> Observed:
    return await _storekit_reject(
        client,
        session,
        storekit,
        path="/v1/subscription/sync",
        error=InvalidTransactionError("forged"),
    )


async def sub_sync_unknown_product(
    client: AsyncClient, session: AsyncSession, storekit: FakeStoreKitVerifier, **_: Any
) -> Observed:
    return await _storekit_reject(
        client, session, storekit, path="/v1/subscription/sync", product="never.configured"
    )


async def sub_sync_product_not_in_channel(
    client: AsyncClient, session: AsyncSession, storekit: FakeStoreKitVerifier, **_: Any
) -> Observed:
    # A subscription product this channel does not sell (it is CloudPayments-only).
    return await _storekit_reject(
        client, session, storekit, path="/v1/subscription/sync", product=PRODUCT_SUB_RU_ONLY
    )


# --- StoreKit: token_purchase ---------------------------------------------------------------------
async def token_verification_unavailable(
    client: AsyncClient, session: AsyncSession, storekit: FakeStoreKitVerifier, **_: Any
) -> Observed:
    return await _storekit_reject(
        client,
        session,
        storekit,
        path="/v1/tokens/purchase",
        error=VerificationUnavailableError("no Apple root CA mounted"),
    )


async def token_invalid_transaction(
    client: AsyncClient, session: AsyncSession, storekit: FakeStoreKitVerifier, **_: Any
) -> Observed:
    return await _storekit_reject(
        client,
        session,
        storekit,
        path="/v1/tokens/purchase",
        error=InvalidTransactionError("forged"),
    )


async def token_unknown_product(
    client: AsyncClient, session: AsyncSession, storekit: FakeStoreKitVerifier, **_: Any
) -> Observed:
    return await _storekit_reject(
        client, session, storekit, path="/v1/tokens/purchase", product="never.configured"
    )


async def token_product_not_in_channel(
    client: AsyncClient, session: AsyncSession, storekit: FakeStoreKitVerifier, **_: Any
) -> Observed:
    return await _storekit_reject(
        client, session, storekit, path="/v1/tokens/purchase", product=PRODUCT_TOKENS_RU_ONLY
    )


async def token_subscription_required(
    client: AsyncClient, session: AsyncSession, storekit: FakeStoreKitVerifier, **_: Any
) -> Observed:
    """The user PAID Apple and we deliberately refuse to credit ⇒ the money must be REFUNDED."""
    return await _storekit_reject(
        client,
        session,
        storekit,
        path="/v1/tokens/purchase",
        product=PRODUCT_TOKENS,
        subscription=None,  # no active subscription → the policy guard refuses
    )


# --- Adapty webhook -------------------------------------------------------------------------------
async def webhook_empty_body(client: AsyncClient, session: AsyncSession, **_: Any) -> Observed:
    return await _adapty(client, session, body=b"", granting=False, seed_device=False)


async def webhook_invalid_json(client: AsyncClient, session: AsyncSession, **_: Any) -> Observed:
    return await _adapty(
        client, session, body=b"{not json", granting=None, broke_our_side=True, seed_device=False
    )


async def webhook_not_an_object(client: AsyncClient, session: AsyncSession, **_: Any) -> Observed:
    return await _adapty(
        client, session, body=b"[1,2,3]", granting=None, broke_our_side=True, seed_device=False
    )


async def webhook_missing_event_id(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    body = adapty_body("ignored")
    del body["profile_event_id"]
    return await _adapty(
        client, session, body=body, granting=None, broke_our_side=True, seed_device=False
    )


async def webhook_missing_customer_user_id(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    return await _adapty(client, session, body=adapty_body("E1", customer=None), granting=True)


async def webhook_unknown_event_type(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    return await _adapty(
        client, session, body=adapty_body("E2", event_type="some_platform_noise"), granting=False
    )


async def webhook_unknown_product(client: AsyncClient, session: AsyncSession, **_: Any) -> Observed:
    return await _adapty(
        client,
        session,
        body=adapty_body("E3", product="never.configured"),
        granting=True,
        result="rejected",
    )


async def webhook_product_not_in_channel(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    return await _adapty(
        client,
        session,
        body=adapty_body("E4", product=PRODUCT_SUB_APPLE_ONLY),
        granting=True,
        result="rejected",
    )


async def webhook_missing_transaction_id(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    return await _adapty(
        client, session, body=adapty_body("E5", txn=None), granting=True, result="rejected"
    )


async def webhook_user_not_found(client: AsyncClient, session: AsyncSession, **_: Any) -> Observed:
    """REMEDIATION IS EXECUTED: fix the resolve (the device mapping), re-process THE SAME event,
    and see whether the grant happens. It does ⇒ ``credit_after_fix`` ⇒ ``lost_payment``."""
    body = adapty_body("E6")
    response = await client.post(ADAPTY_WEBHOOK, json=body, headers=ADAPTY_AUTH)
    assert response.json()["reason"] == "user_not_found"

    # --- the executable remediation: repair OUR side, replay the stored event, no new user action
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    replay = await client.post(ADAPTY_WEBHOOK, json=adapty_body("E6-retry"), headers=ADAPTY_AUTH)
    granted_after_fix = replay.json()["result"] == "applied" and await has_ledger_key(
        session, user_id, "sub-grant:T-imp"
    )

    return Observed(
        http_status=response.status_code,
        external_claims_payment=True,
        channel_authenticated=True,
        parsed_granting=True,
        credited=False,  # at the moment of the branch nothing was credited
        broke_our_side=False,
        remediation="credit_after_fix" if granted_after_fix else "investigate",
        result="ignored",
        channel="adapty",
    )


async def webhook_duplicate_delivery(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    """THE regression that would bury the main alert: on a re-delivery the payer IS credited (by an
    earlier event of the same period) ⇒ ``credited=True`` ⇒ ``impact=none``. Reading ``credits==0``
    of THIS branch instead would fire ``BillingLostPayment`` on every retry of every webhook."""
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    await client.post(ADAPTY_WEBHOOK, json=adapty_body("E7"), headers=ADAPTY_AUTH)
    response = await client.post(ADAPTY_WEBHOOK, json=adapty_body("E7"), headers=ADAPTY_AUTH)
    assert response.json()["reason"] == "duplicate_delivery"

    return Observed(
        http_status=response.status_code,
        external_claims_payment=True,
        channel_authenticated=True,
        parsed_granting=True,
        credited=await has_ledger_key(session, user_id, "sub-grant:T-imp"),
        result="duplicate",
        channel="adapty",
    )


async def webhook_granted(client: AsyncClient, session: AsyncSession, **_: Any) -> Observed:
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    response = await client.post(ADAPTY_WEBHOOK, json=adapty_body("E8"), headers=ADAPTY_AUTH)
    assert response.json()["reason"] == "granted"
    return Observed(
        http_status=response.status_code,
        external_claims_payment=True,
        channel_authenticated=True,
        parsed_granting=True,
        credited=await has_ledger_key(session, user_id, "sub-grant:T-imp"),
        result="applied",
        channel="adapty",
    )


# --- CloudPayments webhook ---
async def webhook_not_a_completed_payment(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    await seed_user(session, device_id=DEVICE_UPPER)
    with respx.mock:
        route = cp_verify_route([cp_payment()])
        response = await client.post(CP_WEBHOOK, json=cp_body(Status="Declined"))
        called = route.call_count
    return Observed(
        http_status=response.status_code,
        verifier_completed=called > 0,
        external_claims_payment=False,  # the callback itself says no money was taken
        parsed_granting=False,
        result="ignored",
        channel="cloudpayments",
    )


async def webhook_invalid_account_id(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    """A COMPLETED payment with an unusable AccountId: money is claimed, and there is no addressee.

    Remediation is EXECUTED: there is nothing on our side to repair (the identifier is garbage), so
    a replay cannot produce a grant.
    """
    with respx.mock:
        route = cp_verify_route([cp_payment()])
        response = await client.post(CP_WEBHOOK, json=cp_body(account_id="not-a-uuid"))
        called = route.call_count
        # remediation: replay the SAME stored event after "fixing" our side — no grant is possible.
        user_id = await seed_user(session, device_id=DEVICE_UPPER)
        replay = await client.post(CP_WEBHOOK, json=cp_body(account_id="not-a-uuid"))
        granted_after_fix = await has_ledger_key(session, user_id, "cp-txn:pay-imp")
        assert replay.status_code == 200

    return Observed(
        http_status=response.status_code,
        verifier_completed=called > 0,
        external_claims_payment=True,
        parsed_granting=None,  # the payer cannot be recognised at all
        credited=False,
        remediation="credit_after_fix" if granted_after_fix else "investigate",
        result="ignored",
        channel="cloudpayments",
    )


async def webhook_cp_user_not_found(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    """money=CLAIMED (the verifier was NOT called — anti-amplification puts resolve first), and the
    remediation IS executable: create the device mapping, replay the stored callback → the grant
    happens ⇒ ``lost_payment``. The verifier's call counter is what refutes ``money=confirmed``."""
    with respx.mock:
        route = cp_verify_route([cp_payment()])
        response = await client.post(CP_WEBHOOK, json=cp_body())
        verifier_calls_before_branch = route.call_count

        user_id = await seed_user(session, device_id=DEVICE_UPPER)
        await client.post(CP_WEBHOOK, json=cp_body())  # the aggregator re-delivers; we did not ask
        granted_after_fix = await has_ledger_key(session, user_id, "cp-txn:pay-imp")

    return Observed(
        http_status=response.status_code,
        verifier_completed=verifier_calls_before_branch > 0,
        external_claims_payment=True,
        parsed_granting=None,
        credited=False,
        remediation="credit_after_fix" if granted_after_fix else "investigate",
        result="ignored",
        channel="cloudpayments",
    )


async def webhook_verify_failed(client: AsyncClient, session: AsyncSession, **_: Any) -> Observed:
    """The upstream is down. We answer 500 ⇒ the aggregator RE-DELIVERS ⇒ nothing is lost yet, and
    no remediation on stored data is possible (we hold no confirmation)."""
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    with respx.mock:
        respx.get(url__regex=rf"{CLOUDPAYMENTS_API_BASE}/users/.*/payments").mock(
            side_effect=httpx.TimeoutException("down")
        )
        response = await client.post(CP_WEBHOOK, json=cp_body())
    return Observed(
        http_status=response.status_code,
        verifier_completed=False,
        external_claims_payment=True,
        parsed_granting=None,
        credited=await has_ledger_key(session, user_id, "cp-txn:pay-imp"),
        broke_our_side=True,  # OUR integration cannot complete an operation it must be able to
        result="error",
        channel="cloudpayments",
    )


async def webhook_no_creditable_payment(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    """Our own verify confirms NOTHING (this is where a forged callback dies). Nothing to credit,
    and a replay cannot invent a confirmation ⇒ investigate."""
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    with respx.mock:
        route = cp_verify_route([])
        response = await client.post(CP_WEBHOOK, json=cp_body())
        replay = await client.post(CP_WEBHOOK, json=cp_body())
        granted_after_fix = await has_ledger_key(session, user_id, "cp-txn:pay-imp")
        assert route.call_count == 2 and replay.status_code == 200

    return Observed(
        http_status=response.status_code,
        verifier_completed=False,  # it ran, but confirmed NO payment
        external_claims_payment=True,
        parsed_granting=None,
        credited=False,
        remediation="credit_after_fix" if granted_after_fix else "investigate",
        result="ignored",
        channel="cloudpayments",
    )


async def webhook_unknown_payment_type(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    """OUR OWN verify CONFIRMED the payment, but its class is one we do not model ⇒ confirmed money,
    zero credits. Reporting this as a benign "duplicate" would show the on-call a healthy path while
    the payer got nothing."""
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    with respx.mock:
        route = cp_verify_route([cp_payment(payment_type="donation")])
        response = await client.post(CP_WEBHOOK, json=cp_body())
        called = route.call_count

    return Observed(
        http_status=response.status_code,
        verifier_completed=called > 0,
        external_claims_payment=True,
        parsed_granting=True,
        credited=await has_ledger_key(session, user_id, "cp-txn:pay-imp"),
        result="rejected",
        channel="cloudpayments",
    )


async def webhook_cp_unknown_product(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    with respx.mock:
        route = cp_verify_route([cp_payment(product_code="never.configured")])
        response = await client.post(CP_WEBHOOK, json=cp_body())
        called = route.call_count
    return Observed(
        http_status=response.status_code,
        verifier_completed=called > 0,
        external_claims_payment=True,
        parsed_granting=True,
        credited=await has_ledger_key(session, user_id, "cp-txn:pay-imp"),
        result="rejected",
        channel="cloudpayments",
    )


async def webhook_cp_product_not_in_channel(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    with respx.mock:
        route = cp_verify_route([cp_payment(product_code=PRODUCT_SUB_APPLE_ONLY)])
        response = await client.post(CP_WEBHOOK, json=cp_body())
        called = route.call_count
    return Observed(
        http_status=response.status_code,
        verifier_completed=called > 0,
        external_claims_payment=True,
        parsed_granting=True,
        credited=await has_ledger_key(session, user_id, "cp-txn:pay-imp"),
        result="rejected",
        channel="cloudpayments",
    )


async def webhook_not_configured(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, **_: Any
) -> Observed:
    """OUR configuration is missing ⇒ we cannot verify ⇒ 500 ⇒ the aggregator keeps re-delivering.
    The payment waits; it is not lost."""
    await seed_user(session, device_id=DEVICE_UPPER)
    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", "")
    get_settings.cache_clear()
    try:
        response = await client.post(CP_WEBHOOK, json=cp_body())
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    return Observed(
        http_status=response.status_code,
        verifier_completed=False,
        external_claims_payment=True,
        parsed_granting=None,
        credited=False,
        broke_our_side=True,
        result="error",
        channel="cloudpayments",
    )


# --- Checkout ---
async def checkout_unknown_product(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    user_id = await seed_user(session)
    response = await client.post(
        CP_CHECKOUT,
        json={"productId": "never.configured", "customerEmail": "a@b.c"},
        headers=auth_headers(user_id),
    )
    return Observed(
        http_status=response.status_code,
        external_claims_payment=False,  # nothing has been paid yet — there is only a link request
        parsed_granting=False,
        result="rejected",
        channel="cloudpayments",
    )


async def checkout_product_not_in_channel(
    client: AsyncClient, session: AsyncSession, **_: Any
) -> Observed:
    user_id = await seed_user(session)
    response = await client.post(
        CP_CHECKOUT,
        json={"productId": PRODUCT_SUB_APPLE_ONLY, "customerEmail": "a@b.c"},
        headers=auth_headers(user_id),
    )
    return Observed(
        http_status=response.status_code,
        external_claims_payment=False,
        parsed_granting=False,
        result="rejected",
        channel="cloudpayments",
    )


async def checkout_not_configured(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, **_: Any
) -> Observed:
    """The instance simply does not sell through this channel — a legitimate state, not a fault."""
    user_id = await seed_user(session)
    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", "")
    get_settings.cache_clear()
    try:
        response = await client.post(
            CP_CHECKOUT,
            json={"productId": PRODUCT_SUB, "customerEmail": "a@b.c"},
            headers=auth_headers(user_id),
        )
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    return Observed(
        http_status=response.status_code,
        external_claims_payment=False,
        parsed_granting=False,
        broke_our_side=False,
        result="rejected",
        channel="cloudpayments",
    )


async def checkout_upstream_error(client: AsyncClient, session: AsyncSession, **_: Any) -> Observed:
    user_id = await seed_user(session)
    with respx.mock:
        respx.post(f"{CLOUDPAYMENTS_API_BASE}/payments/link").mock(
            side_effect=httpx.TimeoutException("down")
        )
        response = await client.post(
            CP_CHECKOUT,
            json={"productId": PRODUCT_SUB, "customerEmail": "a@b.c"},
            headers=auth_headers(user_id),
        )
    return Observed(
        http_status=response.status_code,
        external_claims_payment=False,
        parsed_granting=False,
        broke_our_side=True,  # the aggregator is down: nothing to pay WITH
        result="error",
        channel="cloudpayments",
    )


SCENARIOS: list[Scenario] = [
    Scenario("subscription_sync", "verification_unavailable", sub_sync_verification_unavailable),
    Scenario("subscription_sync", "invalid_transaction", sub_sync_invalid_transaction),
    Scenario("subscription_sync", "unknown_product", sub_sync_unknown_product),
    Scenario("subscription_sync", "product_not_in_channel", sub_sync_product_not_in_channel),
    Scenario("token_purchase", "verification_unavailable", token_verification_unavailable),
    Scenario("token_purchase", "invalid_transaction", token_invalid_transaction),
    Scenario("token_purchase", "unknown_product", token_unknown_product),
    Scenario("token_purchase", "product_not_in_channel", token_product_not_in_channel),
    Scenario("token_purchase", "subscription_required", token_subscription_required),
    Scenario("webhook", "empty_body", webhook_empty_body),
    Scenario("webhook", "invalid_json", webhook_invalid_json),
    Scenario("webhook", "not_an_object", webhook_not_an_object),
    Scenario("webhook", "missing_event_id", webhook_missing_event_id),
    Scenario("webhook", "missing_customer_user_id", webhook_missing_customer_user_id),
    Scenario("webhook", "unknown_event_type", webhook_unknown_event_type),
    Scenario("webhook", "unknown_product", webhook_unknown_product),
    Scenario("webhook", "product_not_in_channel", webhook_product_not_in_channel),
    Scenario("webhook", "missing_transaction_id", webhook_missing_transaction_id),
    Scenario("webhook", "user_not_found", webhook_user_not_found),
    Scenario("webhook", "not_a_completed_payment", webhook_not_a_completed_payment),
    Scenario("webhook", "invalid_account_id", webhook_invalid_account_id),
    Scenario("webhook", "verify_failed", webhook_verify_failed),
    Scenario("webhook", "no_creditable_payment", webhook_no_creditable_payment),
    Scenario("webhook", "unknown_payment_type", webhook_unknown_payment_type),
    Scenario("webhook", "not_configured", webhook_not_configured),
    Scenario("checkout", "unknown_product", checkout_unknown_product),
    Scenario("checkout", "product_not_in_channel", checkout_product_not_in_channel),
    Scenario("checkout", "not_configured", checkout_not_configured),
    Scenario("checkout", "upstream_error", checkout_upstream_error),
]

# The CloudPayments channel reaches `unknown_product` / `product_not_in_channel` through a
# DIFFERENT code path (its own verify), so both are exercised on both channels.
CP_EXTRA: list[Scenario] = [
    Scenario("webhook", "unknown_product", webhook_cp_unknown_product, note="cloudpayments"),
    Scenario(
        "webhook", "product_not_in_channel", webhook_cp_product_not_in_channel, note="cloudpayments"
    ),
    Scenario("webhook", "user_not_found", webhook_cp_user_not_found, note="cloudpayments"),
]

NEUTRAL: list[Scenario] = [
    Scenario("webhook", "duplicate_delivery", webhook_duplicate_delivery),
    Scenario("webhook", "granted", webhook_granted),
]


def _ids(scenarios: list[Scenario]) -> list[str]:
    return [f"{s.op}:{s.reason}{'@' + s.note if s.note else ''}" for s in scenarios]


ALL = SCENARIOS + CP_EXTRA + NEUTRAL


@pytest.mark.parametrize("scenario", ALL, ids=_ids(ALL))
async def test_declared_impact_equals_the_computed_one(
    scenario: Scenario,
    client: AsyncClient,
    session: AsyncSession,
    fake_storekit: FakeStoreKitVerifier,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    declared = impact_for(scenario.op, scenario.reason)

    with caplog.at_level(logging.DEBUG, logger="app.billing.outcome"):
        observed = await scenario.run(
            client=client, session=session, storekit=fake_storekit, monkeypatch=monkeypatch
        )

    computed = reference_impact(
        money=derive_money(observed),  # type: ignore[arg-type]
        credited=observed.credited,
        deliberate=derive_deliberate(observed),
        system_broken=observed.broke_our_side,
        remediation=observed.remediation,  # type: ignore[arg-type]
    )

    assert computed == declared, (
        f"({scenario.op}, {scenario.reason}) declares impact={declared!r} but the total function "
        f"computes {computed!r} from the observed facts: money={derive_money(observed)}, "
        f"credited={observed.credited}, deliberate={derive_deliberate(observed)}, "
        f"system_broken={observed.broke_our_side}, remediation={observed.remediation}"
    )

    # R-OBS-7: the LOG LEVEL is derived from impact, not kept as a second list.
    records = [
        r for r in caplog.records if getattr(r, "extra_fields", {}).get("reason") == scenario.reason
    ]
    assert records, f"no outcome log was emitted for {scenario.reason}"
    expected_level = (
        logging.ERROR
        if observed.result == "error"
        else logging.WARNING
        if declared != "none"
        else logging.INFO
    )
    assert records[-1].levelno == expected_level


@pytest.mark.parametrize("scenario", SCENARIOS + CP_EXTRA, ids=_ids(SCENARIOS + CP_EXTRA))
async def test_every_exit_path_emits_exactly_one_outcome_sample(
    scenario: Scenario,
    client: AsyncClient,
    session: AsyncSession,
    fake_storekit: FakeStoreKitVerifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-OBS-1: an alert over a metric that is silent on the path it exists for is a DEAD alert."""
    declared = impact_for(scenario.op, scenario.reason)
    observed_before = None

    def _sample(channel: str, result: str) -> float:
        return metric_value(
            "billing_outcome_total",
            channel=channel,
            op=scenario.op,
            result=result,
            impact=declared,
            reason=scenario.reason,
        )

    observed = await scenario.run(
        client=client, session=session, storekit=fake_storekit, monkeypatch=monkeypatch
    )
    assert observed_before is None
    assert _sample(observed.channel, observed.result) >= 1.0, (
        f"billing_outcome_total is silent on ({scenario.op}, {scenario.reason}) — "
        "an alert built on it would never fire"
    )


async def test_user_not_found_is_invisible_to_the_journal_metric(
    client: AsyncClient, session: AsyncSession
) -> None:
    """…which is EXACTLY why the alert hangs on ``billing_outcome_total`` and not on
    ``payment_events_total``: no ``payments`` row exists on this path, so the journal metric is
    silent — while the money is real."""
    before = metric_value(
        "billing_outcome_total",
        channel="adapty",
        op="webhook",
        result="ignored",
        impact="lost_payment",
        reason="user_not_found",
    )
    await client.post(ADAPTY_WEBHOOK, json=adapty_body("E-lost"), headers=ADAPTY_AUTH)
    after = metric_value(
        "billing_outcome_total",
        channel="adapty",
        op="webhook",
        result="ignored",
        impact="lost_payment",
        reason="user_not_found",
    )
    assert after == before + 1

    rows = await session.scalar(text("SELECT count(*) FROM payments"))
    assert int(rows or 0) == 0  # no journal row ⇒ payment_events_total says nothing


def test_every_row_of_the_matrix_is_covered_by_a_scenario() -> None:
    """A pair that nobody exercises has an UNVERIFIED impact — the truth test above would simply
    not see it. Adding a row to the matrix without a scenario fails here."""
    covered = {(s.op, s.reason) for s in SCENARIOS + CP_EXTRA}
    declared = set(_IMPACT)
    assert covered == declared, (
        f"rows with no scenario: {sorted(declared - covered)}; "
        f"scenarios for undeclared rows: {sorted(covered - declared)}"
    )
