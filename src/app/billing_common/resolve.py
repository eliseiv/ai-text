"""``resolve_user()`` — the ONE user resolution of the whole payment circuit.

Payment aggregators put a **deviceId** (the id the client handed them) into their "customer id"
field — NOT our ``userId``. A webhook that only looks in ``users`` therefore drops a REAL payment as
``user_not_found``: the user paid, the credits never arrived.

This exact bug was fixed in CloudPayments and **stayed broken in Adapty**, because the
resolution was DUPLICATED — the fix had nowhere to travel. The same incident happened a second time
in prod. Hence the rule, which is not stylistic:

> **Every payment channel resolves the user through THIS function. A channel-local resolution is
> forbidden.**

Users are NEVER provisioned here: a webhook carries no trusted ``sub``, and the
CloudPayments endpoint is fully public — provisioning would let anyone create users with arbitrary
ids. Unresolved ⇒ ``None`` ⇒ the channel answers ``200 ignored/user_not_found`` + WARNING.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RESOLVED_VIA_USER_ID = "user_id"
RESOLVED_VIA_DEVICE_ID = "device_id"


async def resolve_user(session: AsyncSession, x: str) -> tuple[uuid.UUID, str] | None:
    """Resolve the identifier a payment channel sent us to OUR internal ``userId``.

    Deterministic, first match wins:

    (a) ``x`` is a UUID present in ``users`` → it IS our userId (``resolved_via="user_id"``);
    (b) else ``lower(x)`` matches ``lower(auth_devices.device_id)`` → take the linked ``user_id``
        (``resolved_via="device_id"``) — **the incident fix**;
    (c) else ``None`` → ``user_not_found`` (never provision).

    **Case-insensitive by necessity, not by taste.** ``auth_devices.device_id`` is TEXT and stores
    the deviceId in the CLIENT's casing: iOS sends ``identifierForVendor.uuidString`` in UPPERCASE,
    and CloudPayments forwards ``AccountId`` uppercase as well, while ``str(uuid.UUID)`` is always
    lowercase. An exact match would drop precisely the users this fix exists for.

    ``ORDER BY user_id LIMIT 1`` is **not** cosmetic: ``ix_auth_devices_lower_device_id`` is
    deliberately NOT unique (TD-008), so a casing collision is possible in principle. Without the
    limit, ``scalar_one_or_none()`` would raise ``MultipleResultsFound`` **on the payment path** →
    500 → the aggregator retries → 500 again → the payment hangs forever. With it, the pick is
    deterministic (and a silent arbitrary pick is unacceptable for money — hence ORDER BY, not just
    LIMIT).
    """
    candidate = x.strip()
    if not candidate:
        return None

    # Branch (a): only a well-formed UUID can be our users.id — guard before touching the DB, so a
    # garbage identifier cannot produce a cast error inside a money path.
    try:
        as_uuid = uuid.UUID(candidate)
    except ValueError:
        as_uuid = None
    if as_uuid is not None:
        exists = await session.scalar(
            text("SELECT 1 FROM users WHERE id = :x"), {"x": str(as_uuid)}
        )
        if exists:
            return as_uuid, RESOLVED_VIA_USER_ID

    # Branch (b): the deviceId → userId link, taken ONLY from our own auth_devices — never from the
    # callback body. The client controls WHICH identifier it sends, but not WHO gets credited.
    device_user_id = await session.scalar(
        text(
            "SELECT user_id FROM auth_devices WHERE lower(device_id) = :x "
            "ORDER BY user_id LIMIT 1"
        ),
        {"x": candidate.lower()},
    )
    if device_user_id is not None:
        return uuid.UUID(str(device_user_id)), RESOLVED_VIA_DEVICE_ID

    return None
