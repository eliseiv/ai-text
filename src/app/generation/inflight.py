"""Feeding the ``generations_inflight`` gauge — the data side of the ``GenerationStuck`` alert.

A gauge that nobody ever ``.set()``s is not a metric: the series never appears in the exposition,
and every alert over it is DEAD — ``min_over_time(generations_inflight{status="running"}[10m]) > 0``
matches nothing, forever. The rule tests keep passing (they feed SYNTHETIC series), which is worse
than no test at all: it certifies as observable a path that emits nothing.

So the gauge is refreshed at SCRAPE time, from the same partial index the inflight-guard already
uses (``ix_generations_inflight``) — the count is free, and it cannot drift from reality because it
IS the query, not a counter someone must remember to increment.
"""

from __future__ import annotations

import logging

from app.db import get_sessionmaker
from app.generation.repository import GenerationsRepository
from app.observability.metrics import generations_inflight

logger = logging.getLogger("app.generation.inflight")

# Both unfinished statuses are ALWAYS published, zero included. Publishing only the statuses that
# happen to be present would leave a label stuck at its last non-zero value: the queue drains, the
# gauge keeps reporting the old number, and the alert either never clears or never fires.
_TRACKED_STATUSES = ("pending", "running")


async def refresh_generations_inflight() -> None:
    """Read the current inflight counts and publish them. Never raises — /metrics must stay up.

    A scrape must not fail because the database blinked: an exporter that 500s during an incident
    removes exactly the visibility the incident needs. On error the previous values simply stand,
    and ``/ready`` is the endpoint that reports the DB being down.
    """
    try:
        async with get_sessionmaker()() as session:
            counts = await GenerationsRepository(session).count_inflight_global()
    except Exception:  # noqa: BLE001 - a metrics scrape reports, it never raises
        logger.warning("generations_inflight_refresh_failed", exc_info=True)
        return

    for status in _TRACKED_STATUSES:
        generations_inflight.labels(status=status).set(counts.get(status, 0))
