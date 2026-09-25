"""Client-IP resolution — X-Forwarded-For is attacker-controlled."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import CoreSettings, get_settings
from app.deps import client_ip


class _Request:
    """Minimal Starlette-Request stand-in: only `.client.host` and `.headers` are read."""

    def __init__(self, peer: str | None, headers: dict[str, str] | None = None) -> None:
        self.client = type("C", (), {"host": peer})() if peer is not None else None
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


@pytest.fixture
def with_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _apply(**overrides: object) -> None:
        settings = CoreSettings(**overrides)  # type: ignore[arg-type]
        monkeypatch.setattr("app.deps.get_settings", lambda: settings)

    yield _apply
    get_settings.cache_clear()


def test_untrusted_peer_ignores_forwarding_headers(with_settings: Any) -> None:
    with_settings(TRUSTED_PROXY_IPS="")
    request = _Request("203.0.113.9", {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"})
    assert client_ip(request) == "203.0.113.9"  # type: ignore[arg-type]


def test_trusted_proxy_takes_the_hop_from_the_right(with_settings: Any) -> None:
    with_settings(TRUSTED_PROXY_IPS="10.0.0.0/8", TRUSTED_PROXY_HOP_COUNT=1)
    # The left-most entry is spoofable — the client wrote it himself.
    request = _Request("10.0.0.5", {"X-Forwarded-For": "9.9.9.9, 203.0.113.7, 10.0.0.5"})
    assert client_ip(request) == "203.0.113.7"  # type: ignore[arg-type]


def test_xff_spoofing_left_of_the_trusted_proxy_does_not_move_the_result(
    with_settings: Any,
) -> None:
    with_settings(TRUSTED_PROXY_IPS="10.0.0.0/8", TRUSTED_PROXY_HOP_COUNT=1)
    honest = _Request("10.0.0.5", {"X-Forwarded-For": "203.0.113.7, 10.0.0.5"})
    spoofed = _Request("10.0.0.5", {"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 203.0.113.7, 10.0.0.5"})
    assert client_ip(honest) == client_ip(spoofed) == "203.0.113.7"  # type: ignore[arg-type]


def test_x_real_ip_is_honoured_only_from_a_trusted_peer(with_settings: Any) -> None:
    with_settings(TRUSTED_PROXY_IPS="10.0.0.0/8")
    assert client_ip(_Request("10.0.0.5", {"X-Real-IP": "203.0.113.7"})) == "203.0.113.7"  # type: ignore[arg-type]


def test_no_peer_yields_none(with_settings: Any) -> None:
    with_settings(TRUSTED_PROXY_IPS="10.0.0.0/8")
    assert client_ip(_Request(None, {"X-Forwarded-For": "1.2.3.4"})) is None  # type: ignore[arg-type]


def test_invalid_entries_in_trusted_proxy_ips_are_skipped_not_fatal() -> None:
    settings = CoreSettings(TRUSTED_PROXY_IPS="not-an-ip, 10.0.0.0/8, ")  # type: ignore[arg-type]
    networks = settings.trusted_proxy_networks()
    assert len(networks) == 1
    assert str(networks[0]) == "10.0.0.0/8"


def test_empty_trusted_proxy_ips_means_no_trusted_network() -> None:
    assert CoreSettings(TRUSTED_PROXY_IPS="").trusted_proxy_networks() == ()  # type: ignore[arg-type]
