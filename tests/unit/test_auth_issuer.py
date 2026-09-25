"""Embedded RS256 issuer — fail-closed without a key, verifiable with the public one."""

from __future__ import annotations

import uuid

import jwt as pyjwt
import pytest

from app.auth.issuer import IssuerNotConfiguredError, TokenIssuer, build_jwks
from app.config import CoreSettings
from tests.conftest import JWT_AUDIENCE, JWT_ISSUER, JWT_KID, JWT_PRIVATE_PEM, JWT_PUBLIC_PEM


def _settings(**overrides: object) -> CoreSettings:
    base: dict[str, object] = {
        "JWT_PRIVATE_KEY": JWT_PRIVATE_PEM,
        "JWT_PUBLIC_KEY": JWT_PUBLIC_PEM,
        "JWT_ISSUER": JWT_ISSUER,
        "JWT_AUDIENCE": JWT_AUDIENCE,
        "JWT_KID": JWT_KID,
        "JWT_PRIVATE_KEY_PATH": "",
        "JWT_PUBLIC_KEY_PATH": "",
        "AUTH_ACCESS_TTL_SECONDS": 3600,
    }
    base.update(overrides)
    return CoreSettings(**base)  # type: ignore[arg-type]


def test_issued_token_verifies_with_the_public_key_and_carries_the_contract_claims() -> None:
    issuer = TokenIssuer(_settings())
    user_id = uuid.uuid4()
    token = issuer.issue_access_token(user_id=user_id, device_id="dev-1")

    claims = pyjwt.decode(
        token,
        key=JWT_PUBLIC_PEM,
        algorithms=["RS256"],
        issuer=JWT_ISSUER,
        audience=JWT_AUDIENCE,
    )
    assert claims["sub"] == str(user_id)
    assert claims["device_id"] == "dev-1"
    assert claims["exp"] > claims["iat"]
    assert pyjwt.get_unverified_header(token)["kid"] == JWT_KID
    assert pyjwt.get_unverified_header(token)["alg"] == "RS256"


def test_issuer_without_a_private_key_is_not_configured_and_refuses_to_sign() -> None:
    issuer = TokenIssuer(_settings(JWT_PRIVATE_KEY=""))
    assert issuer.configured is False
    with pytest.raises(IssuerNotConfiguredError):
        issuer.issue_access_token(user_id=uuid.uuid4(), device_id="dev-1")


def test_private_key_file_path_wins_over_the_escaped_string(tmp_path: object) -> None:
    path = tmp_path / "key.pem"  # type: ignore[operator]
    path.write_text(JWT_PRIVATE_PEM, encoding="utf-8")
    settings = _settings(JWT_PRIVATE_KEY="ignored-garbage", JWT_PRIVATE_KEY_PATH=str(path))
    assert settings.resolve_private_key() == JWT_PRIVATE_PEM


def test_escaped_newlines_in_the_env_string_become_a_valid_pem() -> None:
    single_line = JWT_PRIVATE_PEM.replace("\n", "\\n")
    assert _settings(JWT_PRIVATE_KEY=single_line).resolve_private_key() == JWT_PRIVATE_PEM


def test_jwks_exposes_only_public_material_and_the_contract_fields() -> None:
    document = build_jwks(JWT_PUBLIC_PEM, JWT_KID)
    key = document["keys"][0]  # type: ignore[index]
    assert set(key) == {"kty", "use", "alg", "kid", "n", "e"}
    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert key["kid"] == JWT_KID
    # No private material may ever appear (d/p/q/dp/dq/qi are the RSA private components).
    assert not {"d", "p", "q", "dp", "dq", "qi"} & set(key)
