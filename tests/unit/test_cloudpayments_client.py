"""CloudPayments boundary: observational auth, the verify client, the checkout client.

The rule these encode: **never design a webhook's authorization from the aggregator's docs — only
from the REAL request.** This aggregator signs nothing, so the callback is only a trigger; the auth
dependency therefore never raises, it only OBSERVES (so we would notice if that ever changed).
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import httpx
import pytest
import respx

from app.billing_cloudpayments.auth import (
    _auth_scheme_label,
    _extract_credential,
    require_cloudpayments_webhook,
)
from app.billing_cloudpayments.checkout import CloudPaymentsCheckoutClient
from app.billing_cloudpayments.verify import (
    CloudPaymentsVerifyClient,
    payment_statuses,
    select_creditable_payments,
)
from app.config import CoreSettings, get_settings
from app.errors import CloudPaymentsVerificationUnavailableError, UpstreamError
from tests.conftest import CLOUDPAYMENTS_API_BASE, metric_value

DEVICE = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"


def _settings(**overrides: Any) -> CoreSettings:
    base: dict[str, Any] = {
        "CLOUDPAYMENTS_API_BASE": CLOUDPAYMENTS_API_BASE,
        "CLOUDPAYMENTS_APP_ID": "app-1",
        "CLOUDPAYMENTS_API_TOKEN": "cp-api-token",
    }
    base.update(overrides)
    return CoreSettings(**base)


# --- observational auth --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("Bearer abc", "abc"),
        ("token abc", "abc"),
        ("abc", "abc"),  # LENIENT: a strict "Bearer <token>" parser 401'd a VALID secret
        # A bare scheme word with nothing after it is treated as a RAW token: leniency is the
        # point here — a strict parser once 401'd a valid secret and lost every RU payment.
        ("Bearer ", "Bearer"),
    ],
)
def test_credential_extraction_is_lenient(header: str | None, expected: str | None) -> None:
    assert _extract_credential(header) == expected


@pytest.mark.parametrize(
    ("header", "label"),
    [(None, "none"), ("", "empty"), ("Bearer x", "bearer"), ("rawtoken", "raw")],
)
def test_scheme_label_records_the_word_never_the_value(header: str | None, label: str) -> None:
    assert _auth_scheme_label(header) == label


class _Request:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def test_webhook_auth_never_raises_and_logs_only_header_names(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLOUDPAYMENTS_WEBHOOK_TOKEN", "legacy-secret")
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.INFO, logger="app.billing_cloudpayments.auth"):
            assert (
                require_cloudpayments_webhook(
                    _Request({"authorization": "Bearer legacy-secret", "x-signature": "sig"})  # type: ignore[arg-type]
                )
                is None
            )
    finally:
        get_settings.cache_clear()

    [record] = [r for r in caplog.records if r.message == "cloudpayments_webhook_auth_observed"]
    fields = record.extra_fields  # type: ignore[attr-defined]
    assert fields["matched"] is True
    assert fields["authScheme"] == "bearer"
    assert fields["presentAuthHeaders"] == ["authorization", "x-signature"]
    assert "legacy-secret" not in str(fields)  # the VALUE is never logged


def test_webhook_auth_accepts_a_request_without_any_authorization(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # This is the real shape of the callback: authScheme=none, no signature.
    with caplog.at_level(logging.INFO, logger="app.billing_cloudpayments.auth"):
        assert require_cloudpayments_webhook(_Request({})) is None  # type: ignore[arg-type]


# --- the verify client (the TRUST ANCHOR) ---------------------------------------------------------
@respx.mock
async def test_404_means_no_payments_not_a_retry() -> None:
    respx.get(f"{CLOUDPAYMENTS_API_BASE}/users/{DEVICE}/payments").mock(
        return_value=httpx.Response(404)
    )
    assert await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=DEVICE) == []


@respx.mock
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(500), "non_2xx"),
        (httpx.Response(200, text="not json"), "malformed"),
        (httpx.Response(200, json=[1, 2]), "malformed"),
        (httpx.Response(200, json={"data": "not a list"}), "malformed"),
    ],
    ids=["non_2xx", "unparseable", "not_an_object", "data_not_a_list"],
)
async def test_verify_failures_raise_a_retriable_error_and_count(
    response: httpx.Response, reason: str
) -> None:
    """Each sample of ``cloudpayments_verify_errors_total`` == one retriable 500 == one payment
    WAITING, not lost."""
    before = metric_value("cloudpayments_verify_errors_total", reason=reason)
    respx.get(f"{CLOUDPAYMENTS_API_BASE}/users/{DEVICE}/payments").mock(return_value=response)

    with pytest.raises(CloudPaymentsVerificationUnavailableError):
        await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=DEVICE)

    assert metric_value("cloudpayments_verify_errors_total", reason=reason) == before + 1


@respx.mock
@pytest.mark.parametrize(
    "error", [httpx.TimeoutException("t"), httpx.ConnectError("c")], ids=["timeout", "connect"]
)
async def test_network_failures_are_retriable(error: Exception) -> None:
    before = metric_value("cloudpayments_verify_errors_total", reason="timeout")
    respx.get(f"{CLOUDPAYMENTS_API_BASE}/users/{DEVICE}/payments").mock(side_effect=error)

    with pytest.raises(CloudPaymentsVerificationUnavailableError):
        await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=DEVICE)

    assert metric_value("cloudpayments_verify_errors_total", reason="timeout") == before + 1


@respx.mock
async def test_non_dict_items_are_dropped() -> None:
    respx.get(f"{CLOUDPAYMENTS_API_BASE}/users/{DEVICE}/payments").mock(
        return_value=httpx.Response(200, json={"data": [{"payment_id": "p"}, "junk", 5]})
    )
    payments = await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=DEVICE)
    assert payments == [{"payment_id": "p"}]


def test_payment_statuses_projection() -> None:
    assert payment_statuses([{"status": "succeeded"}, {"status": "pending"}]) == [
        "succeeded",
        "pending",
    ]


def test_freshness_window_falls_back_to_the_default_instead_of_disabling_itself() -> None:
    # A non-positive window would credit a user's ENTIRE payment history on the first callback.
    assert (
        CoreSettings(CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS=0).cloudpayments_payment_freshness_hours
        == 72
    )
    assert (
        CoreSettings(CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS=-5).cloudpayments_payment_freshness_hours
        == 72
    )


def test_paid_statuses_can_never_be_emptied() -> None:
    assert CoreSettings(CLOUDPAYMENTS_PAID_STATUSES="").cloudpayments_paid_statuses() == frozenset(
        {"succeeded"}
    )
    assert CoreSettings(
        CLOUDPAYMENTS_PAID_STATUSES="[bad json"
    ).cloudpayments_paid_statuses() == frozenset({"succeeded"})
    assert CoreSettings(
        CLOUDPAYMENTS_PAID_STATUSES='["Succeeded", "paid"]'
    ).cloudpayments_paid_statuses() == frozenset({"succeeded", "paid"})
    assert CoreSettings(
        CLOUDPAYMENTS_PAID_STATUSES="succeeded, PAID"
    ).cloudpayments_paid_statuses() == frozenset({"succeeded", "paid"})


def test_select_creditable_payments_is_pure() -> None:
    now = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    data = [
        {
            "payment_id": "p1",
            "status": "SUCCEEDED",
            "paid_at": (now - datetime.timedelta(hours=2)).isoformat(),
            "product": {"code": "sub.monthly", "payment_type": "SUBSCRIPTION"},
        }
    ]
    [payment] = select_creditable_payments(
        data, paid_statuses=frozenset({"succeeded"}), now=now, freshness_hours=72
    )
    assert payment.payment_id == "p1"
    assert payment.payment_type == "subscription"  # normalised


# --- the checkout client ---
@respx.mock
async def test_checkout_maps_the_upstream_response() -> None:
    respx.post(f"{CLOUDPAYMENTS_API_BASE}/payments/link").mock(
        return_value=httpx.Response(
            200,
            json={
                "payment_id": "p-1",
                "payment_url": "https://pay.example.test/p/1",
                "status": "created",
                "expires_at": "2030-01-01T00:00:00Z",
            },
        )
    )
    import uuid

    result = await CloudPaymentsCheckoutClient(_settings()).create_payment_link(
        user_id=uuid.uuid4(), product_id="sub.monthly", customer_email="a@b.c"
    )
    assert result.payment_url == "https://pay.example.test/p/1"
    assert result.expires_at == "2030-01-01T00:00:00Z"


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json={"status": "created"}),  # no payment_url — unusable
        httpx.Response(502),
    ],
    ids=["unparseable", "not_an_object", "no_payment_url", "bad_status"],
)
async def test_unusable_checkout_responses_become_a_plain_502(response: httpx.Response) -> None:
    import uuid

    respx.post(f"{CLOUDPAYMENTS_API_BASE}/payments/link").mock(return_value=response)
    with pytest.raises(UpstreamError):
        await CloudPaymentsCheckoutClient(_settings()).create_payment_link(
            user_id=uuid.uuid4(), product_id="sub.monthly", customer_email="a@b.c"
        )
