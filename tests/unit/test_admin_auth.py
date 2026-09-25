"""Admin token authorization — isolated secret, constant-time, rotation-aware."""

from __future__ import annotations

import pytest

from app.api_gateway.auth import _admin_token_matches, require_admin
from app.config import get_settings
from app.errors import UnauthorizedError


@pytest.fixture(autouse=True)
def _clear_settings() -> object:
    yield
    get_settings.cache_clear()


def _configure(monkeypatch: pytest.MonkeyPatch, current: str, previous: str = "") -> None:
    monkeypatch.setenv("ADMIN_API_SECRET", current)
    monkeypatch.setenv("ADMIN_API_SECRET_PREV", previous)
    get_settings.cache_clear()


def test_current_secret_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, "s3cret")
    assert _admin_token_matches("s3cret") is True


def test_previous_secret_matches_during_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, "new", "old")
    assert _admin_token_matches("old") is True
    assert _admin_token_matches("new") is True


def test_wrong_secret_does_not_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, "s3cret")
    assert _admin_token_matches("nope") is False


def test_empty_configured_secret_never_authorizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: an unset ADMIN_API_SECRET must not turn a blank header into a valid admin."""
    _configure(monkeypatch, "", "")
    assert _admin_token_matches("") is False
    assert _admin_token_matches("anything") is False


async def test_require_admin_rejects_a_missing_header(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, "s3cret")
    with pytest.raises(UnauthorizedError):
        await require_admin(None)


async def test_require_admin_rejects_a_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, "s3cret")
    with pytest.raises(UnauthorizedError):
        await require_admin("wrong")


async def test_require_admin_accepts_the_right_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, "s3cret")
    assert await require_admin("s3cret") is None
