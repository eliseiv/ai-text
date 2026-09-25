"""Human-readable ``accountId`` — a DERIVATION, not stored data.

``8472-1936-AXQ5``. Every property here is load-bearing:

* **deterministic** — the same ``user_id`` always yields the same id ⇒ no sync, no migration, no
  backfill, no unique index, no insert-time collisions;
* **not stored** — it cannot drift out of sync with ``user_id``, because it IS ``user_id`` in
  another representation. Storing it would be storing a function of existing data;
* **alphabet without ``I``/``O``** — the user reads this to support over the PHONE, and ``I``/``1``,
  ``O``/``0`` are indistinguishable by ear;
* **not invertible** without brute force — it does not leak the internal UUID.

It is a display id, never an authorization key: authorization is always the JWT ``sub``.
"""

from __future__ import annotations

import hashlib
import uuid

# No I, O, 0, 1 — dictated aloud, they collide.
_ALPHANUM = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def account_id(user_id: uuid.UUID) -> str:
    """Map a user UUID to the stable display id ``XXXX-XXXX-XXXXX``. Pure."""
    digest = hashlib.sha256(str(user_id).encode("utf-8")).digest()
    g1 = int.from_bytes(digest[0:4], "big") % 10000
    g2 = int.from_bytes(digest[4:8], "big") % 10000
    value = int.from_bytes(digest[8:16], "big")
    chars = []
    for _ in range(5):
        value, rem = divmod(value, len(_ALPHANUM))
        chars.append(_ALPHANUM[rem])
    return f"{g1:04d}-{g2:04d}-{''.join(chars)}"
