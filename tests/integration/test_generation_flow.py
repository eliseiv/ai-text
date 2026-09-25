"""``POST /v1/generate`` — the order of steps IS the module (AC-2/3/4)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.generation.contract import ProviderError
from app.generation.registry import get_pricing, get_provider
from tests.conftest import FakeGenerationProvider, auth_headers, balance_of, metric_value, seed_user


async def _generation(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT status, credits_charged, billing_kind, ledger_tx_id, completed_at, "
                "error_code, meta, units, provider, kind FROM generations WHERE user_id = :u"
            ),
            {"u": str(user_id)},
        )
    ).first()
    if row is None:
        return None
    return {
        "status": row[0],
        "credits_charged": int(row[1]),
        "billing_kind": row[2],
        "ledger_tx_id": row[3],
        "completed_at": row[4],
        "error_code": row[5],
        "meta": row[6],
        "units": int(row[7]),
        "provider": row[8],
        "kind": row[9],
    }


@pytest.fixture(autouse=True)
def _reset_pricing_cache() -> Any:
    yield
    get_pricing.cache_clear()
    get_provider.cache_clear()
    get_settings.cache_clear()


# --- STEP 1: the policy gate ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("subscription", "trial_used", "balance", "reason"),
    [
        (None, True, 0, "trial_used"),
        ("expired", True, 1000, "subscription_expired"),
        ("active", True, 0, "credits_empty"),
    ],
)
async def test_blocked_is_http_200_and_creates_no_row(
    client: AsyncClient,
    session: AsyncSession,
    fake_provider: FakeGenerationProvider,
    subscription: str | None,
    trial_used: bool,
    balance: int,
    reason: str,
) -> None:
    user_id = await seed_user(
        session, subscription=subscription, trial_used=trial_used, balance=balance
    )
    response = await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))

    assert response.status_code == 200  # a business block is NOT an error
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blockReason"] == reason
    assert body["creditsCharged"] == 0

    assert await _generation(session, user_id) is None  # every row means the provider WAS called
    assert fake_provider.calls == []
    assert await balance_of(session, user_id) == balance


async def test_balance_below_the_price_blocks_before_the_provider_runs(
    client: AsyncClient,
    session: AsyncSession,
    fake_provider: FakeGenerationProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """balance=1, price=3 → blocked. Otherwise the upstream would spend real money and the debit
    would then find nothing (BR-3)."""
    monkeypatch.setenv("PRICING_MODE", "flat")
    monkeypatch.setenv("PRICING_FLAT_CREDITS", "3")
    get_settings.cache_clear()
    get_pricing.cache_clear()

    user_id = await seed_user(session, subscription="active", balance=1)
    response = await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))

    assert response.json()["blockReason"] == "credits_empty"
    assert fake_provider.calls == []


async def test_policy_effective_agrees_with_the_generation(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-7: one decision function — the UI can never say "you may" while the generation blocks."""
    for subscription, trial_used, balance in [
        (None, False, 0),
        (None, True, 0),
        ("active", True, 0),
        ("active", True, 5),
        ("expired", True, 5),
    ]:
        user_id = await seed_user(
            session, subscription=subscription, trial_used=trial_used, balance=balance
        )
        headers = auth_headers(user_id)
        effective = (await client.get("/v1/policy/effective", headers=headers)).json()
        generated = (await client.post("/v1/generate", json={"params": {}}, headers=headers)).json()

        assert effective["allowed"] == (generated["status"] != "blocked")
        if not effective["allowed"]:
            assert effective["reasons"] == [generated["blockReason"]]


# --- STEP 3/4: success, debit, accounting -------------------------------------------------------
async def test_success_charges_and_records_everything(
    client: AsyncClient, session: AsyncSession, fake_provider: FakeGenerationProvider
) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    response = await client.post(
        "/v1/generate",
        json={"params": {"prompt": "hi"}, "model": "m1"},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["creditsCharged"] == 1
    assert body["newBalance"] == 9
    assert body["output"] == {"text": "ok"}

    row = await _generation(session, user_id)
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["credits_charged"] == 1
    assert row["billing_kind"] == "credits"
    assert row["ledger_tx_id"] is not None  # no money outside the ledger
    assert row["completed_at"] is not None
    assert row["provider"] == fake_provider.name
    assert await balance_of(session, user_id) == 9

    ledger = (
        await session.execute(
            text(
                "SELECT type, amount, idempotency_key FROM ledger_transactions "
                "WHERE user_id = :u"
            ),
            {"u": str(user_id)},
        )
    ).all()
    assert len(ledger) == 1
    assert ledger[0][0] == "debit" and int(ledger[0][1]) == 1


async def test_idempotent_replay_does_not_charge_twice(
    client: AsyncClient, session: AsyncSession, fake_provider: FakeGenerationProvider
) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    headers = {**auth_headers(user_id), "Idempotency-Key": "req-1"}

    first = (await client.post("/v1/generate", json={"params": {}}, headers=headers)).json()
    second = (await client.post("/v1/generate", json={"params": {}}, headers=headers)).json()

    assert first["generationId"] == second["generationId"]
    assert second["idempotentReplay"] is True
    assert second["creditsCharged"] == 0
    assert await balance_of(session, user_id) == 9  # charged exactly once
    assert len(fake_provider.calls) == 1  # and the provider ran exactly once


async def test_running_generation_with_the_same_key_is_409_already_in_progress(
    client: AsyncClient, session: AsyncSession, fake_provider: FakeGenerationProvider
) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    await session.execute(
        text(
            "INSERT INTO generations (user_id, kind, provider, status, idempotency_key) "
            "VALUES (:u, 'echo', 'echo', 'running', 'busy-key')"
        ),
        {"u": str(user_id)},
    )
    await session.commit()

    response = await client.post(
        "/v1/generate",
        json={"params": {}},
        headers={**auth_headers(user_id), "Idempotency-Key": "busy-key"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_in_progress"
    assert fake_provider.calls == []


async def test_inflight_guard_is_a_different_409(
    client: AsyncClient, session: AsyncSession, fake_provider: FakeGenerationProvider
) -> None:
    """Two 409s, two causes, two client reactions — they MUST be distinguishable by code."""
    user_id = await seed_user(session, subscription="active", balance=10)
    for i in range(3):  # GENERATION_MAX_INFLIGHT_PER_USER = 3
        await session.execute(
            text(
                "INSERT INTO generations (user_id, kind, provider, status, idempotency_key) "
                "VALUES (:u, 'echo', 'echo', 'running', :k)"
            ),
            {"u": str(user_id), "k": f"inflight-{i}"},
        )
    await session.commit()

    response = await client.post(
        "/v1/generate",
        json={"params": {}},
        headers={**auth_headers(user_id), "Idempotency-Key": "new-key"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "too_many_inflight"
    assert fake_provider.calls == []

    count = await session.scalar(
        text("SELECT count(*) FROM generations WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert int(count or 0) == 3  # NO row was created for the refused request


async def test_inflight_guard_can_be_disabled(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GENERATION_MAX_INFLIGHT_PER_USER", "0")
    get_settings.cache_clear()

    user_id = await seed_user(session, subscription="active", balance=10)
    for i in range(5):
        await session.execute(
            text(
                "INSERT INTO generations (user_id, kind, provider, status, idempotency_key) "
                "VALUES (:u, 'echo', 'echo', 'running', :k)"
            ),
            {"u": str(user_id), "k": f"inflight-{i}"},
        )
    await session.commit()

    response = await client.post(
        "/v1/generate",
        json={"params": {}},
        headers={**auth_headers(user_id), "Idempotency-Key": "fresh"},
    )
    assert response.status_code == 200, response.text


async def test_provider_error_costs_nothing(
    client: AsyncClient, session: AsyncSession, fake_provider: FakeGenerationProvider
) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    fake_provider.error = ProviderError(
        "upstream_timeout", status_code=504, provider_error_type="timeout", retryable=True
    )

    response = await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"

    row = await _generation(session, user_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["credits_charged"] == 0
    assert row["error_code"] == "upstream_timeout"
    assert row["ledger_tx_id"] is None
    assert await balance_of(session, user_id) == 10
    ledger = await session.scalar(
        text("SELECT count(*) FROM ledger_transactions WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert int(ledger or 0) == 0


async def test_failed_generation_may_be_retried_on_the_same_key(
    client: AsyncClient, session: AsyncSession, fake_provider: FakeGenerationProvider
) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    headers = {**auth_headers(user_id), "Idempotency-Key": "retry-key"}
    fake_provider.error = ProviderError("upstream_error")
    assert (
        await client.post("/v1/generate", json={"params": {}}, headers=headers)
    ).status_code == 502

    fake_provider.error = None
    retried = await client.post("/v1/generate", json={"params": {}}, headers=headers)
    assert retried.status_code == 200
    row = await _generation(session, user_id)
    assert row is not None and row["status"] == "succeeded"
    assert row["error_code"] is None  # cleared: ck_generations_error_code allows it only on failed


async def test_trial_is_free_atomic_and_once(client: AsyncClient, session: AsyncSession) -> None:
    user_id = await seed_user(session, trial_used=False, subscription=None, balance=0)
    first = await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))
    assert first.status_code == 200
    assert first.json()["status"] == "succeeded"
    assert first.json()["creditsCharged"] == 0

    row = await _generation(session, user_id)
    assert row is not None and row["billing_kind"] == "trial" and row["credits_charged"] == 0
    trial_used = await session.scalar(
        text("SELECT trial_used FROM users WHERE id = :u"), {"u": str(user_id)}
    )
    assert trial_used is True

    second = await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))
    assert second.json()["blockReason"] == "trial_used"


# --- unbilled: the service worked for free (impact=revenue_loss) ---------------------------------
async def test_underestimated_quote_ends_unbilled_not_negative(
    client: AsyncClient,
    session: AsyncSession,
    fake_provider: FakeGenerationProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``quote()`` said 1, the provider produced 5 units → the debit finds no credits. The user
    keeps the result and never goes negative; the SERVICE eats the cost — and that path must be
    VISIBLE (``billing_kind='unbilled'`` → ``impact=revenue_loss`` → ``GenerationUnbilled``)."""
    monkeypatch.setenv("PRICING_MODE", "units")
    monkeypatch.setenv("PRICING_UNITS", "{}")  # rate 1 → quote 1, charge = units
    get_settings.cache_clear()
    get_pricing.cache_clear()

    fake_provider.usage_units = 5
    user_id = await seed_user(session, subscription="active", balance=1)

    before = metric_value(
        "generation_total", kind="echo", provider="echo", status="succeeded", impact="revenue_loss"
    )
    with caplog.at_level(logging.WARNING, logger="app.generation.service"):
        response = await client.post(
            "/v1/generate", json={"params": {}}, headers=auth_headers(user_id)
        )

    assert response.status_code == 200
    assert response.json()["creditsCharged"] == 0
    row = await _generation(session, user_id)
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["billing_kind"] == "unbilled"
    assert row["credits_charged"] == 0
    assert await balance_of(session, user_id) == 1  # NOT negative, NOT charged

    after = metric_value(
        "generation_total", kind="echo", provider="echo", status="succeeded", impact="revenue_loss"
    )
    assert after == before + 1
    assert any(r.message == "generation_unbilled" for r in caplog.records)


async def test_zero_price_on_a_delivered_result_is_unbilled_not_a_paid_success(
    client: AsyncClient,
    session: AsyncSession,
    fake_provider: FakeGenerationProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A price of 0 with a DELIVERED result is the service working for free.

    Labelling it ``billing_kind='credits'`` (impact=none) makes a free hand-out indistinguishable
    from a paid success and hides it from ``GenerationUnbilled`` — the alert whose entire purpose
    is to see it. Causes in the wild: a mis-configured PRICING_UNITS rate, a provider reporting
    ``units=0``, a flat price of 0.
    """
    monkeypatch.setenv("PRICING_MODE", "flat")
    monkeypatch.setenv("PRICING_FLAT_CREDITS", "0")
    get_settings.cache_clear()
    get_pricing.cache_clear()

    user_id = await seed_user(session, subscription="active", balance=10)
    before = metric_value(
        "generation_total", kind="echo", provider="echo", status="succeeded", impact="revenue_loss"
    )
    response = await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))

    assert response.status_code == 200
    row = await _generation(session, user_id)
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["billing_kind"] == "unbilled"
    assert row["credits_charged"] == 0
    assert await balance_of(session, user_id) == 10
    after = metric_value(
        "generation_total", kind="echo", provider="echo", status="succeeded", impact="revenue_loss"
    )
    assert after == before + 1


# --- meta / logging -------------------------------------------------------------------------------
async def test_large_output_is_truncated_in_meta(
    client: AsyncClient,
    session: AsyncSession,
    fake_provider: FakeGenerationProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GENERATION_META_MAX_BYTES", "256")
    get_settings.cache_clear()

    fake_provider.output = {"blob": "x" * 5000}
    user_id = await seed_user(session, subscription="active", balance=10)
    response = await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))

    assert response.status_code == 200
    assert response.json()["output"] == {"blob": "x" * 5000}  # the caller still gets it
    row = await _generation(session, user_id)
    assert row is not None
    assert row["meta"].get("truncated") is True  # …but the DB stores a projection, never the blob
    assert "output" not in row["meta"]


async def test_params_and_output_never_reach_the_logs(
    client: AsyncClient,
    session: AsyncSession,
    fake_provider: FakeGenerationProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_provider.output = {"secret_output": "TOP-SECRET-OUTPUT"}
    user_id = await seed_user(session, subscription="active", balance=10)

    with caplog.at_level(logging.DEBUG):
        await client.post(
            "/v1/generate",
            json={"params": {"prompt": "TOP-SECRET-PROMPT"}},
            headers=auth_headers(user_id),
        )

    dumped = json.dumps(
        [{"msg": r.getMessage(), "fields": getattr(r, "extra_fields", {})} for r in caplog.records],
        default=str,
    )
    assert "TOP-SECRET-PROMPT" not in dumped
    assert "TOP-SECRET-OUTPUT" not in dumped


# --- DB-level money invariants (they hold even when the application is wrong) ---------------------
async def test_database_refuses_credits_on_a_failed_generation(session: AsyncSession) -> None:
    user_id = await seed_user(session)
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO generations (user_id, kind, provider, status, idempotency_key, "
                "credits_charged, completed_at) "
                "VALUES (:u, 'echo', 'echo', 'failed', 'k', 5, now())"
            ),
            {"u": str(user_id)},
        )
        await session.flush()
    await session.rollback()


async def test_database_refuses_charged_credits_without_a_ledger_link(
    session: AsyncSession,
) -> None:
    user_id = await seed_user(session)
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO generations (user_id, kind, provider, status, idempotency_key, "
                "credits_charged, completed_at) "
                "VALUES (:u, 'echo', 'echo', 'succeeded', 'k', 5, now())"
            ),
            {"u": str(user_id)},
        )
        await session.flush()
    await session.rollback()


async def test_database_requires_completed_at_on_a_terminal_status(
    session: AsyncSession,
) -> None:
    user_id = await seed_user(session)
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO generations (user_id, kind, provider, status, idempotency_key) "
                "VALUES (:u, 'echo', 'echo', 'succeeded', 'k')"
            ),
            {"u": str(user_id)},
        )
        await session.flush()
    await session.rollback()


async def test_database_refuses_an_error_code_on_a_non_failed_generation(
    session: AsyncSession,
) -> None:
    user_id = await seed_user(session)
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO generations (user_id, kind, provider, status, idempotency_key, "
                "error_code, completed_at) "
                "VALUES (:u, 'echo', 'echo', 'succeeded', 'k', 'boom', now())"
            ),
            {"u": str(user_id)},
        )
        await session.flush()
    await session.rollback()


# --- reads ---
async def test_generation_reads_are_owner_scoped(
    client: AsyncClient, session: AsyncSession
) -> None:
    owner = await seed_user(session, subscription="active", balance=10)
    stranger = await seed_user(session, subscription="active", balance=10)
    created = (
        await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(owner))
    ).json()

    mine = await client.get(
        f"/v1/generations/{created['generationId']}", headers=auth_headers(owner)
    )
    assert mine.status_code == 200
    assert mine.json()["output"] == {"text": "ok"}

    # A foreign generation is a 404, never a 403: we do not reveal that someone else's row exists.
    theirs = await client.get(
        f"/v1/generations/{created['generationId']}", headers=auth_headers(stranger)
    )
    assert theirs.status_code == 404
    assert theirs.json()["error"]["code"] == "generation_not_found"

    listing = await client.get("/v1/generations", headers=auth_headers(stranger))
    assert listing.json()["items"] == []


async def test_generation_list_filters(client: AsyncClient, session: AsyncSession) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))

    unfiltered = await client.get("/v1/generations", headers=auth_headers(user_id))
    assert unfiltered.status_code == 200  # regression: the unfiltered list used to 500
    assert len(unfiltered.json()["items"]) == 1

    valid = await client.get("/v1/generations?status=succeeded", headers=auth_headers(user_id))
    assert valid.status_code == 200 and len(valid.json()["items"]) == 1

    empty = await client.get("/v1/generations?status=failed", headers=auth_headers(user_id))
    assert empty.status_code == 200 and empty.json()["items"] == []


@pytest.mark.parametrize("value", ["bogus", "Succeeded", "SUCCEEDED", "1"])
async def test_status_outside_the_enum_is_422_not_500(
    client: AsyncClient, session: AsyncSession, value: str
) -> None:
    """A value the DB cannot cast to ``generation_status`` used to reach PostgreSQL and 500."""
    user_id = await seed_user(session)
    response = await client.get(f"/v1/generations?status={value}", headers=auth_headers(user_id))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_generation_stats(client: AsyncClient, session: AsyncSession) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))
    stats = await client.get("/v1/generations/stats", headers=auth_headers(user_id))
    assert stats.status_code == 200
    [total] = stats.json()["totals"]
    assert total["kind"] == "echo"
    assert total["generations"] == 1 and total["succeeded"] == 1
    assert total["creditsCharged"] == 1
