"""File storage for originals (PDF, pasted text, web snapshots) + temporary signed links.

Local volume shared by ``api`` and ``worker``. Keys are built ONLY by the server
(``{user_id}/{source_id}/…``) and validated again here — no client string ever reaches a path.

Links: ``/v1/files/{token}`` where the token is ``base64url(payload).hmac``; payload carries the
source id, the owner and the expiry. The download route re-checks in the DB that the source still
exists — a link to a deleted source/topic stops working immediately, not at expiry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_KEY_RE = re.compile(r"^[0-9a-f-]{36}/[0-9a-f-]{36}/[a-z0-9_.-]{1,64}$")


class LocalFileStorage:
    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise ValueError("invalid storage key")
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise ValueError("storage key escapes the root")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic: a reader never sees a half-written file

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def path(self, key: str) -> Path:
        return self._path(key)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete_source(self, user_id: uuid.UUID, source_id: uuid.UUID) -> None:
        """Remove every file of a source. Idempotent."""
        directory = (self._root / str(user_id) / str(source_id)).resolve()
        if self._root in directory.parents and directory.exists():
            shutil.rmtree(directory, ignore_errors=True)


def source_key(user_id: uuid.UUID, source_id: uuid.UUID, name: str) -> str:
    return f"{user_id}/{source_id}/{name}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class LinkClaims:
    source_id: uuid.UUID
    user_id: uuid.UUID
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_link(
    secret: str, *, source_id: uuid.UUID, user_id: uuid.UUID, ttl_seconds: int
) -> tuple[str, int]:
    expires = int(time.time()) + ttl_seconds
    payload = _b64(json.dumps({"s": str(source_id), "u": str(user_id), "e": expires}).encode())
    sig = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}", expires


def verify_link(secret: str, token: str) -> LinkClaims | None:
    """``None`` for anything forged, malformed or expired — never an exception to the caller."""
    if not secret or token.count(".") != 1:
        return None
    payload, sig = token.split(".")
    expected = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
        claims = LinkClaims(uuid.UUID(data["s"]), uuid.UUID(data["u"]), int(data["e"]))
    except (ValueError, KeyError, TypeError):
        return None
    if claims.expires_at < time.time():
        return None
    return claims
