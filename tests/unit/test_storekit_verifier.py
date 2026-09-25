"""StoreKit JWS verification — REAL certificate chains, not a mocked verifier.

⚠️ These tests build actual X.509 chains with ``cryptography`` and sign actual ES256 JWS tokens.
Mocking the verifier here would make the whole file worthless: the bug class it exists for
("every signature verifies, the chain anchors to a trusted root, and yet the leaf is forged") is
invisible to a mock — it lives in the difference between a chain VALIDATOR and a signature
CALCULATOR.

Regression guards (both MUST fail against the pre-fix verifier):

* a **forged leaf** signed by a NON-CA certificate that is itself legitimately issued under the
  trusted root → 422 (without the ``BasicConstraints CA=TRUE`` check every signature verifies and
  the forgery is accepted);
* a **Sandbox** transaction on an instance configured as ``Production`` → 422 (a sandbox purchase
  is FREE but carries a genuine Apple signature and the same bundleId ⇒ free money).
"""

from __future__ import annotations

import base64
import datetime
from typing import Any

import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from app.config import get_settings
from app.errors import InvalidTransactionError, VerificationUnavailableError
from app.subscription.storekit import StoreKitVerifier

_NOW = datetime.datetime.now(tz=datetime.UTC)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(
    *,
    cn: str,
    key: ec.EllipticCurvePrivateKey,
    issuer_name: x509.Name,
    issuer_key: ec.EllipticCurvePrivateKey,
    ca: bool,
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or (_NOW - datetime.timedelta(days=1)))
        .not_valid_after(not_after or (_NOW + datetime.timedelta(days=365)))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    return builder.sign(issuer_key, hashes.SHA256())


class _Pki:
    """A miniature Apple-shaped PKI: root → intermediate → leaf, all real."""

    def __init__(self) -> None:
        self.root_key = ec.generate_private_key(ec.SECP256R1())
        self.root = _cert(
            cn="Test Apple Root CA",
            key=self.root_key,
            issuer_name=_name("Test Apple Root CA"),
            issuer_key=self.root_key,
            ca=True,
        )
        self.inter_key = ec.generate_private_key(ec.SECP256R1())
        self.inter = _cert(
            cn="Test Apple Intermediate",
            key=self.inter_key,
            issuer_name=self.root.subject,
            issuer_key=self.root_key,
            ca=True,
        )
        self.leaf_key = ec.generate_private_key(ec.SECP256R1())
        self.leaf = _cert(
            cn="Test Apple Leaf",
            key=self.leaf_key,
            issuer_name=self.inter.subject,
            issuer_key=self.inter_key,
            ca=False,
        )

    def expired_intermediate(self) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
        key = ec.generate_private_key(ec.SECP256R1())
        cert = _cert(
            cn="Expired Intermediate",
            key=key,
            issuer_name=self.root.subject,
            issuer_key=self.root_key,
            ca=True,
            not_before=_NOW - datetime.timedelta(days=800),
            not_after=_NOW - datetime.timedelta(days=1),  # expired yesterday
        )
        return cert, key

    def end_entity_under_root(self) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
        """A NON-CA certificate legitimately issued under the trusted root.

        This is the attacker's asset in the forged-leaf scenario: he holds its private key.
        """
        key = ec.generate_private_key(ec.SECP256R1())
        cert = _cert(
            cn="Some End Entity Under The Root",
            key=key,
            issuer_name=self.root.subject,
            issuer_key=self.root_key,
            ca=False,
        )
        return cert, key


def _b64(cert: x509.Certificate) -> str:
    return base64.b64encode(cert.public_bytes(Encoding.DER)).decode()


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "transactionId": "txn-1",
        "originalTransactionId": "txn-1",
        "productId": "sub.monthly",
        "bundleId": "com.example.app",
        "environment": "Production",
        "expiresDate": int((_NOW + datetime.timedelta(days=30)).timestamp() * 1000),
    }
    body.update(overrides)
    return body


def _jws(
    payload: dict[str, Any],
    *,
    signing_key: ec.EllipticCurvePrivateKey,
    chain: list[x509.Certificate],
) -> str:
    return pyjwt.encode(
        payload,
        signing_key,
        algorithm="ES256",
        headers={"x5c": [_b64(c) for c in chain]},
    )


@pytest.fixture
def pki() -> _Pki:
    return _Pki()


@pytest.fixture
def verifier(pki: _Pki, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> StoreKitVerifier:
    """A REAL verifier trusting only our test root (mounted as a .cer, exactly like prod)."""
    (tmp_path / "AppleRootCA.cer").write_bytes(pki.root.public_bytes(Encoding.DER))
    monkeypatch.setenv("APPSTORE_ROOT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("APPSTORE_ENVIRONMENT", "Production")
    monkeypatch.setenv("APPSTORE_BUNDLE_ID", "com.example.app")
    monkeypatch.setenv("STOREKIT_TEST_MODE", "false")
    get_settings.cache_clear()
    built = StoreKitVerifier()
    get_settings.cache_clear()
    return built


# --- the happy path ---------------------------------------------------------------------------
def test_valid_production_chain_is_accepted(verifier: StoreKitVerifier, pki: _Pki) -> None:
    jws = _jws(_payload(), signing_key=pki.leaf_key, chain=[pki.leaf, pki.inter, pki.root])
    tx = verifier.verify(jws)
    assert tx.transaction_id == "txn-1"
    assert tx.product_id == "sub.monthly"
    assert tx.environment == "production"
    assert tx.revoked is False
    assert tx.expires_at is not None


def test_revocation_date_marks_the_transaction_revoked(
    verifier: StoreKitVerifier, pki: _Pki
) -> None:
    jws = _jws(
        _payload(revocationDate=1_700_000_000_000),
        signing_key=pki.leaf_key,
        chain=[pki.leaf, pki.inter, pki.root],
    )
    assert verifier.verify(jws).revoked is True


# --- REGRESSION 1: sandbox transaction on a production instance --------------------------------
def test_sandbox_transaction_on_a_production_instance_is_rejected(
    verifier: StoreKitVerifier, pki: _Pki
) -> None:
    """A sandbox Apple ID pays NOTHING, yet the transaction is genuinely Apple-signed and carries
    the same bundleId. Accepting it on a production instance is free money."""
    jws = _jws(
        _payload(environment="Sandbox"),
        signing_key=pki.leaf_key,
        chain=[pki.leaf, pki.inter, pki.root],
    )
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_environment_comparison_is_case_insensitive(verifier: StoreKitVerifier, pki: _Pki) -> None:
    jws = _jws(
        _payload(environment="PRODUCTION"),
        signing_key=pki.leaf_key,
        chain=[pki.leaf, pki.inter, pki.root],
    )
    assert verifier.verify(jws).environment == "production"


# --- REGRESSION 2: forged leaf signed by a NON-CA cert issued under the trusted root -----------
def test_forged_leaf_signed_by_a_non_ca_certificate_is_rejected(
    verifier: StoreKitVerifier, pki: _Pki
) -> None:
    """THE attack a signature calculator cannot see.

    The attacker holds ANY end-entity certificate issued under the trusted Apple root (they are
    handed out — that is what end-entity certificates are). He mints his own leaf, signs it with
    his key, and presents ``[forged_leaf, his_cert, root]``:

    * forged_leaf → signed by his_cert  ✔ verifies
    * his_cert    → signed by root      ✔ verifies
    * root        → our trusted root    ✔ anchored

    Every link checks out. Only ``BasicConstraints CA=TRUE`` on the ISSUER stops it — and without
    that check ``jwt.decode`` would then trust the forged leaf's public key, i.e. accept a
    completely fabricated StoreKit transaction.
    """
    attacker_cert, attacker_key = pki.end_entity_under_root()
    forged_leaf_key = ec.generate_private_key(ec.SECP256R1())
    forged_leaf = _cert(
        cn="Forged Leaf",
        key=forged_leaf_key,
        issuer_name=attacker_cert.subject,
        issuer_key=attacker_key,  # signed by a certificate that is NOT a CA
        ca=False,
    )
    jws = _jws(
        _payload(transactionId="forged", productId="sub.monthly"),
        signing_key=forged_leaf_key,
        chain=[forged_leaf, attacker_cert, pki.root],
    )
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


# --- chain validation -------------------------------------------------------------------------
def test_expired_intermediate_is_rejected(verifier: StoreKitVerifier, pki: _Pki) -> None:
    expired_inter, expired_key = pki.expired_intermediate()
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _cert(
        cn="Leaf under expired intermediate",
        key=leaf_key,
        issuer_name=expired_inter.subject,
        issuer_key=expired_key,
        ca=False,
    )
    jws = _jws(_payload(), signing_key=leaf_key, chain=[leaf, expired_inter, pki.root])
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


@pytest.mark.parametrize("length", [2, 4], ids=["too_short", "too_long"])
def test_chain_of_the_wrong_length_is_rejected(
    verifier: StoreKitVerifier, pki: _Pki, length: int
) -> None:
    chain = [pki.leaf, pki.inter, pki.root]
    chain = chain[:2] if length == 2 else [pki.leaf, pki.inter, pki.root, pki.root]
    jws = _jws(_payload(), signing_key=pki.leaf_key, chain=chain)
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_chain_anchored_to_an_untrusted_root_is_rejected(
    verifier: StoreKitVerifier,
) -> None:
    rogue = _Pki()  # a complete, internally valid PKI — just not OUR root
    jws = _jws(_payload(), signing_key=rogue.leaf_key, chain=[rogue.leaf, rogue.inter, rogue.root])
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_signature_not_matching_the_leaf_key_is_rejected(
    verifier: StoreKitVerifier, pki: _Pki
) -> None:
    other_key = ec.generate_private_key(ec.SECP256R1())
    jws = _jws(_payload(), signing_key=other_key, chain=[pki.leaf, pki.inter, pki.root])
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_missing_x5c_is_rejected(verifier: StoreKitVerifier, pki: _Pki) -> None:
    jws = pyjwt.encode(_payload(), pki.leaf_key, algorithm="ES256")
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_unparsable_x5c_is_rejected(verifier: StoreKitVerifier, pki: _Pki) -> None:
    jws = pyjwt.encode(
        _payload(), pki.leaf_key, algorithm="ES256", headers={"x5c": ["not-base64-der"]}
    )
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


# --- payload validation -----------------------------------------------------------------------
def test_bundle_id_mismatch_is_rejected(verifier: StoreKitVerifier, pki: _Pki) -> None:
    jws = _jws(
        _payload(bundleId="com.attacker.app"),
        signing_key=pki.leaf_key,
        chain=[pki.leaf, pki.inter, pki.root],
    )
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_missing_transaction_id_is_rejected(verifier: StoreKitVerifier, pki: _Pki) -> None:
    payload = _payload()
    del payload["transactionId"]
    jws = _jws(payload, signing_key=pki.leaf_key, chain=[pki.leaf, pki.inter, pki.root])
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_non_jws_input_is_rejected(verifier: StoreKitVerifier) -> None:
    with pytest.raises(InvalidTransactionError):
        verifier.verify("not-a-jws")


# --- fail-closed posture ----------------------------------------------------------------------
def test_no_root_ca_mounted_refuses_to_verify(pki: _Pki, monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Cannot verify" means REFUSE — never "trust the client"."""
    monkeypatch.setenv("APPSTORE_ROOT_CERT_DIR", "")
    monkeypatch.setenv("STOREKIT_TEST_MODE", "false")
    get_settings.cache_clear()
    verifier = StoreKitVerifier()
    get_settings.cache_clear()
    jws = _jws(_payload(), signing_key=pki.leaf_key, chain=[pki.leaf, pki.inter, pki.root])
    with pytest.raises(VerificationUnavailableError):
        verifier.verify(jws)


def test_hs256_outside_test_mode_is_rejected_like_a_forgery(
    verifier: StoreKitVerifier,
) -> None:
    jws = pyjwt.encode(_payload(), "some-secret", algorithm="HS256")
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_hs256_inside_test_mode_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOREKIT_TEST_MODE", "true")
    monkeypatch.setenv("STOREKIT_TEST_SECRET", "storekit-test-secret")
    monkeypatch.setenv("APPSTORE_ENVIRONMENT", "Production")
    monkeypatch.setenv("APPSTORE_BUNDLE_ID", "com.example.app")
    get_settings.cache_clear()
    verifier = StoreKitVerifier()
    get_settings.cache_clear()
    jws = pyjwt.encode(_payload(), "storekit-test-secret", algorithm="HS256")
    assert verifier.verify(jws).transaction_id == "txn-1"


# --- more of the fail-closed surface (each line here is a way in for a forged transaction) --------
def test_root_dir_that_does_not_exist_behaves_like_no_root(
    pki: _Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPSTORE_ROOT_CERT_DIR", "D:/no/such/dir")
    monkeypatch.setenv("STOREKIT_TEST_MODE", "false")
    get_settings.cache_clear()
    verifier = StoreKitVerifier()
    get_settings.cache_clear()
    jws = _jws(_payload(), signing_key=pki.leaf_key, chain=[pki.leaf, pki.inter, pki.root])
    with pytest.raises(VerificationUnavailableError):
        verifier.verify(jws)


def test_a_pem_encoded_root_is_loaded_too(
    pki: _Pki, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "root.pem").write_bytes(pki.root.public_bytes(Encoding.PEM))
    (tmp_path / "notes.txt").write_bytes(b"ignored, not a certificate")
    monkeypatch.setenv("APPSTORE_ROOT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("APPSTORE_ENVIRONMENT", "Production")
    monkeypatch.setenv("APPSTORE_BUNDLE_ID", "com.example.app")
    monkeypatch.setenv("STOREKIT_TEST_MODE", "false")
    get_settings.cache_clear()
    verifier = StoreKitVerifier()
    get_settings.cache_clear()

    jws = _jws(_payload(), signing_key=pki.leaf_key, chain=[pki.leaf, pki.inter, pki.root])
    assert verifier.verify(jws).transaction_id == "txn-1"


@pytest.mark.parametrize(
    "header",
    [
        "not-base64-json",
        base64.urlsafe_b64encode(b'["not", "an", "object"]').decode().rstrip("="),
    ],
    ids=["unparsable_header", "header_not_an_object"],
)
def test_broken_jws_headers_are_rejected(verifier: StoreKitVerifier, header: str) -> None:
    with pytest.raises(InvalidTransactionError):
        verifier.verify(f"{header}.body.signature")


def test_x5c_that_is_not_a_list_is_rejected(verifier: StoreKitVerifier, pki: _Pki) -> None:
    jws = pyjwt.encode(
        _payload(), pki.leaf_key, algorithm="ES256", headers={"x5c": "a-single-string"}
    )
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_issuer_without_basic_constraints_is_rejected(
    verifier: StoreKitVerifier, pki: _Pki
) -> None:
    """A certificate that does not even STATE whether it may issue certificates cannot be trusted
    to have issued one."""
    key = ec.generate_private_key(ec.SECP256R1())
    naked = (
        x509.CertificateBuilder()
        .subject_name(_name("No BasicConstraints"))
        .issuer_name(pki.root.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOW - datetime.timedelta(days=1))
        .not_valid_after(_NOW + datetime.timedelta(days=1))
        .sign(pki.root_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _cert(cn="leaf", key=leaf_key, issuer_name=naked.subject, issuer_key=key, ca=False)
    jws = _jws(_payload(), signing_key=leaf_key, chain=[leaf, naked, pki.root])
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_ca_that_may_not_sign_certificates_is_rejected(
    verifier: StoreKitVerifier, pki: _Pki
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    no_cert_sign = (
        x509.CertificateBuilder()
        .subject_name(_name("CA without keyCertSign"))
        .issuer_name(pki.root.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOW - datetime.timedelta(days=1))
        .not_valid_after(_NOW + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,  # ← declared as a CA, but NOT allowed to sign certificates
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(pki.root_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _cert(
        cn="leaf", key=leaf_key, issuer_name=no_cert_sign.subject, issuer_key=key, ca=False
    )
    jws = _jws(_payload(), signing_key=leaf_key, chain=[leaf, no_cert_sign, pki.root])
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_test_mode_rejects_a_wrong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOREKIT_TEST_MODE", "true")
    monkeypatch.setenv("STOREKIT_TEST_SECRET", "storekit-test-secret")
    get_settings.cache_clear()
    verifier = StoreKitVerifier()
    get_settings.cache_clear()
    jws = pyjwt.encode(_payload(), "a-different-secret", algorithm="HS256")
    with pytest.raises(InvalidTransactionError):
        verifier.verify(jws)


def test_transaction_without_an_expiry_is_accepted_with_expires_at_none(
    verifier: StoreKitVerifier, pki: _Pki
) -> None:
    payload = _payload()
    del payload["expiresDate"]
    jws = _jws(payload, signing_key=pki.leaf_key, chain=[pki.leaf, pki.inter, pki.root])
    tx = verifier.verify(jws)
    assert tx.expires_at is None
    assert tx.original_transaction_id == "txn-1"
