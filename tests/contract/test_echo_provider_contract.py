"""The built-in providers must satisfy the very suite a domain will run on its own provider."""

from __future__ import annotations

from app.generation.contract import ProviderError
from app.generation.echo_provider import EchoProvider
from tests.conftest import FakeGenerationProvider
from tests.contract.provider_suite import provider_contract_suite

TestEchoProvider = provider_contract_suite(EchoProvider(), kind="echo")

_fake = FakeGenerationProvider()


def _break_upstream() -> None:
    _fake.error = ProviderError("upstream_timeout", retryable=True, status_code=504)


TestFakeProvider = provider_contract_suite(_fake, kind="echo", fail_upstream=_break_upstream)
