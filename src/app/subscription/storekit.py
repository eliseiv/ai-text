"""StoreKit JWS verification — real cryptography, fail-closed.

Apple signs the transaction as a JWS whose header carries an ``x5c`` certificate chain. We verify
the chain up to a TRUSTED Apple root CA (mounted via ``APPSTORE_ROOT_CERT_DIR``), then verify the
JWS signature with the leaf certificate's public key (ES256), then validate the payload
(``bundleId``, environment).

**Fail-closed is the whole point.** No root CA configured → the verifier REFUSES the transaction
(``422``). The alternative ("if we cannot check it, believe the client") turns the subscription
into a field the client fills in himself — anyone would activate a subscription with one request.

Test-mode (HS256) is honoured ONLY when ``STOREKIT_TEST_MODE`` **and** ``STOREKIT_TEST_SECRET`` are
both set; outside that, an HS256 token is rejected exactly like a forged one. The StoreKit payload
is never logged (redaction covers ``transaction``/``jws``).
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from app.config import get_settings
from app.errors import InvalidTransactionError, VerificationUnavailableError

logger = logging.getLogger("app.subscription.storekit")

# Apple's JWS chain is always leaf → intermediate → root.
_APPLE_CHAIN_LENGTH = 3


@dataclass(frozen=True)
class VerifiedTransaction:
    """Fields extracted from a CRYPTOGRAPHICALLY VERIFIED payload — never from the request body."""

    transaction_id: str
    original_transaction_id: str
    product_id: str
    expires_at: datetime.datetime | None
    revoked: bool
    environment: str


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _jws_header(jws: str) -> dict[str, Any]:
    header_segment = jws.split(".", 1)[0]
    try:
        header = json.loads(_b64url_decode(header_segment))
    except (ValueError, json.JSONDecodeError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise InvalidTransactionError("StoreKit JWS header is not valid base64url JSON") from exc
    if not isinstance(header, dict):
        raise InvalidTransactionError("StoreKit JWS header must be a JSON object")
    return header


def _load_certificate_chain(jws: str) -> list[x509.Certificate]:
    header = _jws_header(jws)
    x5c = header.get("x5c")
    if not x5c or not isinstance(x5c, list):
        raise InvalidTransactionError("StoreKit JWS missing x5c certificate chain")
    try:
        return [x509.load_der_x509_certificate(base64.b64decode(cert)) for cert in x5c]
    except (ValueError, TypeError) as exc:
        raise InvalidTransactionError("StoreKit JWS x5c chain is unparsable") from exc


def _verify_signed_by(child: x509.Certificate, issuer: x509.Certificate) -> None:
    """Cryptographic link only. NOT sufficient on its own — see ``_verify_chain``."""
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    hash_alg = child.signature_hash_algorithm
    if hash_alg is None:
        raise InvalidTransactionError("certificate missing signature hash algorithm")
    pubkey = issuer.public_key()
    if isinstance(pubkey, ec.EllipticCurvePublicKey):
        pubkey.verify(child.signature, child.tbs_certificate_bytes, ec.ECDSA(hash_alg))
    elif isinstance(pubkey, rsa.RSAPublicKey):
        pubkey.verify(child.signature, child.tbs_certificate_bytes, padding.PKCS1v15(), hash_alg)
    else:  # pragma: no cover - Apple uses EC; defensive
        raise InvalidTransactionError("unsupported certificate key type in StoreKit chain")


def _require_ca(cert: x509.Certificate) -> None:
    """An ISSUER must actually be allowed to issue.

    **This check is the difference between a chain validator and a signature calculator.**
    Without it, the holder of ANY end-entity certificate issued under a trusted Apple root can
    sign a forged leaf with his own key and present ``[fake_leaf, his_cert, …, root]``: every
    signature verifies, the chain anchors to our trusted root, and ``jwt.decode`` then trusts the
    forged leaf's public key ⇒ a forged StoreKit transaction is accepted.

    The assumption "nobody under this root holds a key" is exactly the assumption a chain
    validator must never make.
    """
    try:
        basic = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound as exc:
        raise InvalidTransactionError("issuer certificate has no BasicConstraints") from exc
    if not basic.ca:
        raise InvalidTransactionError("issuer certificate is not a CA (BasicConstraints CA=false)")
    try:
        key_usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return  # KeyUsage is optional; CA:TRUE already carries the essential restriction
    if not key_usage.key_cert_sign:
        raise InvalidTransactionError("issuer certificate may not sign certificates")


def _require_valid_now(cert: x509.Certificate, now: datetime.datetime) -> None:
    """Validity window — checked for EVERY link, not just the leaf.

    Skipping it means an expired intermediate keeps working forever, which is precisely what
    expiry exists to prevent.
    """
    if now < cert.not_valid_before_utc or now > cert.not_valid_after_utc:
        raise InvalidTransactionError("certificate in the StoreKit chain is not valid at this time")


def _verify_chain(chain: list[x509.Certificate], roots: list[x509.Certificate]) -> None:
    """Full path validation: shape, validity window, CA constraints, links, trusted anchor.

    Apple's chain is always leaf → intermediate → root. Fixing the length removes the whole class
    of "long chain with a surprise in the middle" and keeps the validation trivially auditable.
    """
    if len(chain) != _APPLE_CHAIN_LENGTH:
        raise InvalidTransactionError(
            "StoreKit certificate chain must be leaf → intermediate → root"
        )

    now = datetime.datetime.now(tz=datetime.UTC)
    for cert in chain:
        _require_valid_now(cert, now)

    # Every ISSUER in the chain (intermediate and root) must be a CA allowed to sign certificates.
    for issuer in chain[1:]:
        _require_ca(issuer)

    for i in range(len(chain) - 1):
        _verify_signed_by(chain[i], chain[i + 1])

    # The chain's root must BE one of our trusted roots — byte-for-byte. Not "signed by one":
    # accepting a chain whose root is merely signed by a trusted root re-opens the same hole.
    root_fingerprints = {r.public_bytes(Encoding.DER) for r in roots}
    if chain[-1].public_bytes(Encoding.DER) not in root_fingerprints:
        raise InvalidTransactionError(
            "StoreKit certificate chain is not anchored to a trusted Apple root"
        )


class StoreKitVerifier:
    """Verifies Apple-signed StoreKit JWS transactions. Shared by subscription + token-purchase."""

    def __init__(self) -> None:
        settings = get_settings()
        self._bundle_id = settings.appstore_bundle_id
        self._environment = settings.appstore_environment
        self._roots = self._load_roots(settings.appstore_root_cert_dir)
        # Active ONLY when the flag AND the secret are both set; never weakens the real path.
        self._test_secret = settings.storekit_test_secret
        self._test_mode = settings.storekit_test_mode and bool(self._test_secret)

    @staticmethod
    def _load_roots(cert_dir: str) -> list[x509.Certificate]:
        if not cert_dir:
            return []
        directory = Path(cert_dir)
        if not directory.is_dir():
            return []
        roots: list[x509.Certificate] = []
        for path in sorted(directory.glob("*")):
            if path.suffix.lower() not in (".cer", ".der", ".pem", ".crt"):
                continue
            data = path.read_bytes()
            try:
                roots.append(x509.load_der_x509_certificate(data))
            except ValueError:
                roots.append(x509.load_pem_x509_certificate(data))
        return roots

    def verify(self, signed_transaction: str) -> VerifiedTransaction:
        """Verify one transaction. ANY failure raises (422) — nothing is ever "assumed ok"."""
        if not isinstance(signed_transaction, str) or signed_transaction.count(".") != 2:
            raise InvalidTransactionError("StoreKit transaction must be a compact JWS string")

        header = _jws_header(signed_transaction)
        alg = str(header.get("alg", ""))

        if alg == "HS256":
            if not self._test_mode:
                # Fail-closed: the test seam is NOT an open door in prod.
                raise InvalidTransactionError("StoreKit JWS signature invalid")
            return self._verify_test_transaction(signed_transaction)

        return self._verify_real_transaction(signed_transaction)

    def _verify_real_transaction(self, signed_transaction: str) -> VerifiedTransaction:
        chain = _load_certificate_chain(signed_transaction)
        leaf = chain[0]

        if not self._roots:
            # No trust anchor → we CANNOT verify. Refuse (422). Distinct error/reason from a bad
            # signature: this one means "our deployment is broken", and paying users are affected.
            raise VerificationUnavailableError(
                "App Store root certificates are not configured (APPSTORE_ROOT_CERT_DIR)"
            )
        _verify_chain(chain, self._roots)

        try:
            payload: dict[str, Any] = jwt.decode(
                signed_transaction,
                key=leaf.public_key(),  # type: ignore[arg-type]
                algorithms=["ES256"],
                options={"verify_aud": False},
            )
        except jwt.InvalidTokenError as exc:
            raise InvalidTransactionError("StoreKit JWS signature invalid") from exc

        return self._normalize_payload(payload)

    def _verify_test_transaction(self, signed_transaction: str) -> VerifiedTransaction:
        """test-mode: HS256 signed with ``STOREKIT_TEST_SECRET``. Same failure semantics (422)."""
        try:
            payload: dict[str, Any] = jwt.decode(
                signed_transaction,
                key=self._test_secret,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
        except jwt.InvalidTokenError as exc:
            raise InvalidTransactionError("StoreKit JWS signature invalid") from exc

        return self._normalize_payload(payload)

    def _normalize_payload(self, payload: dict[str, Any]) -> VerifiedTransaction:
        bundle_id = payload.get("bundleId")
        if self._bundle_id and bundle_id != self._bundle_id:
            raise InvalidTransactionError("StoreKit transaction bundleId mismatch")

        # ⚠ ENVIRONMENT MUST MATCH — this is a MONEY check, not metadata.
        # A Sandbox purchase is FREE (a sandbox Apple ID pays nothing), yet it is signed by a
        # genuine Apple chain and carries the same bundleId. Without this comparison a production
        # instance would accept it, activate the subscription and credit the tokens: free money.
        # Apple sends "Production" / "Sandbox" — compare case-insensitively.
        environment = str(payload.get("environment", self._environment)).strip().lower()
        expected_environment = self._environment.strip().lower()
        if expected_environment and environment != expected_environment:
            raise InvalidTransactionError("StoreKit transaction environment mismatch")

        expires_ms = payload.get("expiresDate")
        expires_at = (
            datetime.datetime.fromtimestamp(int(expires_ms) / 1000, tz=datetime.UTC)
            if expires_ms is not None
            else None
        )
        revoked = payload.get("revocationDate") is not None

        if "transactionId" not in payload:
            raise InvalidTransactionError("StoreKit transaction missing transactionId")

        return VerifiedTransaction(
            transaction_id=str(payload["transactionId"]),
            original_transaction_id=str(
                payload.get("originalTransactionId", payload["transactionId"])
            ),
            product_id=str(payload.get("productId", "")),
            expires_at=expires_at,
            revoked=revoked,
            environment=environment,
        )


_verifier_singleton: StoreKitVerifier | None = None


def get_storekit_verifier() -> StoreKitVerifier:
    global _verifier_singleton
    if _verifier_singleton is None:
        _verifier_singleton = StoreKitVerifier()
    return _verifier_singleton
