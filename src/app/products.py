"""The single product catalogue — ``PRODUCTS``.

Replaces the three near-identical maps of the source (``TOKEN_PRODUCTS`` /
``ADAPTY_PRODUCT_TOKENS`` / ``CLOUDPAYMENTS_PRODUCT_TOKENS``) and their three fallback grants.

Two invariants live here:

* **BR-8 (anti-tamper).** ``credits`` from THIS map is the ONLY source of the granted amount.
  Never the client body, never ``payments.amount``, never a "default" constant.
* **Fail-closed.** A product missing from the map — or not allowed in the paying channel by its
  ``channels`` allowlist — grants **0 credits** (``payments.status='rejected'``, WARNING, alert
  ``PaymentRejected``). There is **no** fallback grant in any channel: crediting an amount nobody
  configured is the same BR-8 violation as taking the amount from the callback body.

``GET /v1/products`` serves the catalogue from this very map, so the UI and the grant cannot
drift apart.
"""

from __future__ import annotations

import calendar
import datetime
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from app.models.tables import PAYMENT_CHANNEL, PAYMENT_KIND

logger = logging.getLogger("app.products")

# Product kinds a purchase may have. `subscription_event` is a payment_kind but NOT a product
# kind (an expiry/cancel event carries no product).
_PRODUCT_KINDS: frozenset[str] = frozenset({"subscription", "tokens"})
_CHANNELS: frozenset[str] = frozenset(PAYMENT_CHANNEL)

# Guard against the two enums drifting apart: every product kind must be a valid payment kind.
# A plain `assert` would vanish under `python -O` — the guard must hold in production too.
if not frozenset(PAYMENT_KIND) >= _PRODUCT_KINDS:
    raise RuntimeError(
        "product kinds drifted from the payment_kind enum: "
        f"{sorted(_PRODUCT_KINDS - frozenset(PAYMENT_KIND))}"
    )


@dataclass(frozen=True)
class Product:
    """One entry of the server-side product catalogue."""

    product_id: str
    kind: str  # 'subscription' | 'tokens'
    credits: int  # > 0. The ONLY source of the granted amount (BR-8)
    channels: frozenset[str]  # allowlist: which channels may sell this product
    title: str  # display name for GET /v1/products (defaults to product_id)
    # ISO-8601 duration of ONE paid subscription period: P7D / P1W / P1M / P1Y. Required only where
    # the channel itself does not say when access ends (CloudPayments sends a payment, not an
    # expiry). ``None`` for token packs.
    period: str | None = None


_PERIOD_RE = re.compile(r"^P([1-9][0-9]{0,2})([DWMY])$")


def parse_period(value: object) -> str | None:
    """``"P1W"`` → ``"P1W"``; anything that is not a single-unit ISO-8601 duration → ``None``."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if _PERIOD_RE.match(normalized) else None


def add_period(start: datetime.datetime, period: str) -> datetime.datetime:
    """``start`` + one period. Months/years are CALENDAR arithmetic (Jan 31 + P1M = Feb 28/29),
    not "30 days" — a yearly plan must last a year, not twelve 30-day blocks."""
    match = _PERIOD_RE.match(period)
    if match is None:
        raise ValueError(f"invalid period {period!r}")
    count, unit = int(match.group(1)), match.group(2)
    if unit == "D":
        return start + datetime.timedelta(days=count)
    if unit == "W":
        return start + datetime.timedelta(weeks=count)
    months = count if unit == "M" else count * 12
    month_index = start.month - 1 + months
    year, month = start.year + month_index // 12, month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def parse_products(raw: str) -> dict[str, Product]:
    """Parse the ``PRODUCTS`` JSON into a validated catalogue. Pure (no I/O).

    Degradation rules (same shape discipline as the source's ``token_products()``):

    * malformed JSON / non-object → ``{}`` — an empty catalogue, in which case EVERY purchase is
      rejected (``422`` / ``payments.status='rejected'``), never a partial/ambiguous credit table;
    * an entry survives only when it is fully valid: ``str`` key, ``kind`` in
      {subscription, tokens}, positive ``int`` ``credits`` (``bool`` is a subclass of ``int`` and
      is excluded so ``true`` cannot become ``1``), and a non-empty ``channels`` list whose values
      are all known payment channels;
    * an invalid entry is dropped with a WARNING — it is a mis-configuration and must be loud,
      not silently compensated.
    """
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, json.JSONDecodeError):
        logger.warning(
            "PRODUCTS is not valid JSON — the catalogue is EMPTY, every purchase is "
            "rejected (fail-closed, BR-8)"
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning("PRODUCTS is not a JSON object — the catalogue is EMPTY (fail-closed)")
        return {}

    products: dict[str, Product] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not key.strip():
            continue
        product_id = key.strip()
        if not isinstance(value, dict):
            logger.warning("PRODUCTS[%s] is not an object — dropped", product_id)
            continue

        kind = value.get("kind")
        if not isinstance(kind, str) or kind not in _PRODUCT_KINDS:
            logger.warning("PRODUCTS[%s] has an unknown kind %r — dropped", product_id, kind)
            continue

        credits = value.get("credits")
        # bool is a subclass of int; exclude it explicitly (True -> 1 would be a silent surprise).
        if isinstance(credits, bool) or not isinstance(credits, int) or credits <= 0:
            logger.warning("PRODUCTS[%s] has invalid credits %r — dropped", product_id, credits)
            continue

        raw_channels = value.get("channels")
        if not isinstance(raw_channels, list) or not raw_channels:
            logger.warning("PRODUCTS[%s] has no channels — dropped", product_id)
            continue
        # Compare SETS, not lengths: a duplicate of a VALID channel (["adapty","adapty"]) is
        # harmless and must NOT drop the product. Only an unknown / non-string channel is
        # fail-closed — a product must never be sellable through a channel nobody allowed.
        if any(not isinstance(c, str) for c in raw_channels):
            logger.warning("PRODUCTS[%s] has a non-string channel — dropped", product_id)
            continue
        declared = {c.strip() for c in raw_channels if isinstance(c, str) and c.strip()}
        unknown = declared - _CHANNELS
        if unknown or not declared:
            logger.warning(
                "PRODUCTS[%s] lists an unknown channel %s — dropped", product_id, sorted(unknown)
            )
            continue
        channels = declared

        period: str | None = None
        if "period" in value:
            period = parse_period(value.get("period"))
            if period is None:
                # Fail-closed and LOUD: a typo must not silently become "some default duration".
                logger.warning(
                    "PRODUCTS[%s] has an invalid period %r (expected P7D/P1W/P1M/P1Y) — dropped",
                    product_id,
                    value.get("period"),
                )
                continue
        if kind == "subscription" and period is None and "cloudpayments" in channels:
            # The RU channel learns about a payment, never about an expiry: without a period the
            # access term would be a guess. Refuse the misconfiguration instead of guessing.
            logger.warning(
                "PRODUCTS[%s] is a cloudpayments subscription without a period — dropped",
                product_id,
            )
            continue

        title = value.get("title")
        products[product_id] = Product(
            product_id=product_id,
            kind=kind,
            credits=credits,
            channels=frozenset(channels),
            title=title if isinstance(title, str) and title else product_id,
            period=period if kind == "subscription" else None,
        )
    return products


@lru_cache
def get_products() -> Mapping[str, Product]:
    """Process-wide product catalogue (parsed once; ``get_settings()`` is itself cached)."""
    from app.config import get_settings  # local import: config imports this module

    return get_settings().products()


def credits_for(product_id: str | None, channel: str) -> int | None:
    """Credits granted by ``product_id`` when bought through ``channel`` — or ``None``.

    ``None`` means **grant nothing** (fail-closed): either the product is unknown, or it
    is not allowed in this channel (a CloudPayments callback naming an Apple ``productId`` gets
    nothing — the hole the source had). The caller marks the payment ``rejected``, 0 credits.
    """
    if not product_id:
        return None
    product = get_products().get(product_id)
    if product is None:
        return None
    if channel not in product.channels:
        return None
    return product.credits


def products_for_channel(channel: str) -> tuple[Product, ...]:
    """Catalogue subset sellable through ``channel`` (source of ``GET /v1/products``)."""
    return tuple(p for p in get_products().values() if channel in p.channels)
