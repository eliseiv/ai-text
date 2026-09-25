"""CloudPayments callback parsing + verify reconciliation.

The callback is a TRIGGER, never a source of truth: card PII is not even read, and its
``TransactionId`` keys nothing.
"""

from __future__ import annotations

import datetime
import json

import pytest

from app.billing_cloudpayments import parser
from app.billing_cloudpayments.verify import select_creditable_payments

_NOW = datetime.datetime(2030, 1, 10, 12, 0, tzinfo=datetime.UTC)


def _callback(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "Status": "Completed",
        "OperationType": "Payment",
        "AccountId": "3F2504E0-4F89-11D3-9A0C-0305E82C3301",
        "TransactionId": 55555,
        "CardFirstSix": "411111",
        "CardLastFour": "1111",
        "CardType": "Visa",
        "Issuer": "Some Bank",
        "Data": json.dumps({"user_id": "fallback-device"}),
    }
    body.update(overrides)
    return body


def test_data_is_a_json_string_in_the_real_payload() -> None:
    assert parser.parse_data({"Data": '{"user_id": "d1"}'}) == {"user_id": "d1"}
    assert parser.parse_data({"Data": {"user_id": "d1"}}) == {"user_id": "d1"}
    assert parser.parse_data({"Data": "not json"}) == {}
    assert parser.parse_data({"Data": "[1,2]"}) == {}
    assert parser.parse_data({}) == {}


def test_gate_accepts_a_completed_payment_or_a_refund() -> None:
    assert parser.parse_gate("completed", "payment") is True
    assert parser.parse_gate("declined", "payment") is False
    # A refund is a trigger too; like a payment it is confirmed through verify before any effect.
    assert parser.parse_gate("completed", "refund") is True
    assert parser.parse_gate("completed", "cancel") is False


def test_status_and_operation_type_are_normalised() -> None:
    body = _callback(Status="COMPLETED", OperationType="Payment")
    assert parser.parse_status(body) == "completed"
    assert parser.parse_operation_type(body) == "payment"


def test_device_id_comes_from_account_id_then_data() -> None:
    assert parser.parse_device_id(_callback(), {}) == "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
    body = _callback()
    del body["AccountId"]
    assert parser.parse_device_id(body, {"user_id": "fallback"}) == "fallback"


def test_transaction_id_is_log_context_only() -> None:
    # Never a key: we do not trust the callback, so its identifier cannot key money.
    assert parser.parse_transaction_id(_callback()) == "55555"


def test_uuid_guard_blocks_ssrf_in_the_verify_path() -> None:
    assert parser.is_uuid("3F2504E0-4F89-11D3-9A0C-0305E82C3301") is True
    assert parser.is_uuid("../../admin") is False
    assert parser.is_uuid("not-a-uuid") is False


def test_payment_type_maps_to_the_payment_kind() -> None:
    assert parser.kind_for_payment_type("one_time") == parser.KIND_TOKENS
    assert parser.kind_for_payment_type("subscription") == parser.KIND_SUBSCRIPTION
    assert parser.kind_for_payment_type("donation") is None  # unmodelled → skip, never guess


def test_card_pii_is_not_even_parsed() -> None:
    """Excluded BY CONSTRUCTION: nothing downstream can leak what was never read."""
    parsed = parser.ParsedCallback(
        device_id=str(parser.parse_device_id(_callback(), {})),
        transaction_id=parser.parse_transaction_id(_callback()),
        status=parser.parse_status(_callback()),
        operation_type=parser.parse_operation_type(_callback()),
    )
    fields = set(parsed.__dataclass_fields__)
    assert not {"card_first_six", "card_last_four", "card_type", "issuer", "amount"} & fields


# --- reconciliation (pure) --------------------------------------------------------------------
def _payment(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "payment_id": "pay-1",
        "status": "succeeded",
        "paid_at": (_NOW - datetime.timedelta(hours=1)).isoformat(),
        "product": {"code": "sub.monthly", "payment_type": "subscription"},
    }
    item.update(overrides)
    return item


def _select(items: list[dict[str, object]], hours: int = 72) -> list[object]:
    return select_creditable_payments(
        items,
        paid_statuses=frozenset({"succeeded"}),
        now=_NOW,
        freshness_hours=hours,
    )


def test_a_fresh_paid_payment_is_creditable() -> None:
    [payment] = _select([_payment()])
    assert payment.payment_id == "pay-1"  # type: ignore[attr-defined]
    assert payment.payment_type == "subscription"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "override",
    [
        {"status": "pending"},
        {"paid_at": (_NOW - datetime.timedelta(days=30)).isoformat()},  # outside the window
        {"paid_at": "not-a-date"},
        {"payment_id": ""},
        {"product": {"code": "", "payment_type": "subscription"}},
        {"product": {"code": "sub.monthly"}},  # no payment_type
        {"product": "not-an-object"},
    ],
    ids=[
        "unpaid_status",
        "stale_payment",
        "unparseable_date",
        "no_payment_id",
        "no_product_code",
        "no_payment_type",
        "product_not_an_object",
    ],
)
def test_unusable_payments_are_never_credited(override: dict[str, object]) -> None:
    assert _select([_payment(**override)]) == []


def test_history_is_not_credited_wholesale_on_the_first_callback() -> None:
    """The freshness window exists so a user with a long payment history does not get his entire
    back-catalogue credited at once on the first callback."""
    old = _payment(payment_id="old", paid_at=(_NOW - datetime.timedelta(days=100)).isoformat())
    fresh = _payment(payment_id="fresh")
    selected = _select([old, fresh])
    assert [p.payment_id for p in selected] == ["fresh"]  # type: ignore[attr-defined]
