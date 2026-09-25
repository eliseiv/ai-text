"""R-OBS-8: a metric that is DECLARED but never published is a DEAD alert.

The bug this file exists for: ``generations_inflight`` was declared, the counting query was
written — and ``.set()`` was called nowhere. The series therefore did not exist in production, and
``GenerationStuck`` (a critical page) could never fire. ``promtool test rules`` passed the whole
time, because it feeds SYNTHETIC series and never checks that anybody publishes the real ones.

**So this test asserts the EXPOSITION, not the rule.** It scrapes ``GET /metrics`` after a real run
and looks for the actual series.
"""

from __future__ import annotations

import re

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import auth_headers, seed_user


def _series(body: str, name: str, **labels: str) -> float | None:
    """Parse one sample out of the Prometheus exposition — the CONSUMER's view of the metric.

    Label ORDER in the exposition is not ours to choose (prometheus_client sorts them), so the
    sample is matched by its label SET, not by a textual prefix.
    """
    for line in body.splitlines():
        if not line.startswith(f"{name}{{"):
            continue
        raw_labels, _, value = line[len(name) + 1 :].rpartition("}")
        found = dict(re.findall(r'(\w+)="([^"]*)"', raw_labels))
        if found == labels:
            return float(value)
    return None


async def test_generations_inflight_is_published_even_at_zero(client: AsyncClient) -> None:
    """The series must EXIST with no generations at all — an alert over a series that does not
    exist matches nothing, forever."""
    body = (await client.get("/metrics")).text
    assert _series(body, "generations_inflight", status="running") == 0.0
    assert _series(body, "generations_inflight", status="pending") == 0.0


async def test_generations_inflight_tracks_reality_and_does_not_stick(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    await session.execute(
        text(
            "INSERT INTO generations (user_id, kind, provider, status, idempotency_key) "
            "VALUES (:u, 'echo', 'echo', 'running', 'stuck-1')"
        ),
        {"u": str(user_id)},
    )
    await session.commit()

    body = (await client.get("/metrics")).text
    assert _series(body, "generations_inflight", status="running") == 1.0

    await session.execute(
        text(
            "UPDATE generations SET status = 'succeeded', completed_at = now() "
            "WHERE idempotency_key = 'stuck-1'"
        )
    )
    await session.commit()

    body = (await client.get("/metrics")).text
    # The label must go back to 0 — a stale 1 would page forever (zeros are published on purpose).
    assert _series(body, "generations_inflight", status="running") == 0.0


async def test_generation_total_is_published_with_the_impact_label(
    client: AsyncClient, session: AsyncSession
) -> None:
    labels = {"kind": "echo", "provider": "echo", "status": "succeeded", "impact": "none"}
    before = _series((await client.get("/metrics")).text, "generation_total", **labels) or 0.0

    user_id = await seed_user(session, subscription="active", balance=10)
    await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))

    after = _series((await client.get("/metrics")).text, "generation_total", **labels)
    assert after == before + 1.0  # the alerting label `impact` is actually EXPOSED


async def test_service_info_identifies_the_instance(client: AsyncClient) -> None:
    """SERVICE_NAME/VERSION are labels of the SERVICE: several template-born services scrape into
    one Prometheus, and without them their series are indistinguishable."""
    app = client.app  # type: ignore[attr-defined]
    async with app.router.lifespan_context(app):
        body = (await client.get("/metrics")).text

    assert (
        _series(
            body,
            "service_info",
            service="service-template-tests",
            version="9.9.9",
            environment="dev",
        )
        == 1.0
    )


async def test_metrics_endpoint_is_token_protected_when_configured(
    client: AsyncClient, monkeypatch: object
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("METRICS_SCRAPE_TOKEN", "scrape-secret")  # type: ignore[attr-defined]
    get_settings.cache_clear()
    try:
        assert (await client.get("/metrics")).status_code == 403
        ok = await client.get("/metrics", headers={"X-Scrape-Token": "scrape-secret"})
        assert ok.status_code == 200
    finally:
        get_settings.cache_clear()
