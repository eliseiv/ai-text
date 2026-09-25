"""``SourcesProvider`` over the fake LLM: what reaches the user is only what the server verified."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.domain.llm import FakeLLMClient, LLMError
from app.domain.prompts import NO_ANSWER_TEXT
from app.domain.provider import SourcesProvider
from app.domain.text import chunk_text
from app.generation.contract import GenerationRequest, ProviderError
from tests.contract.provider_suite import provider_contract_suite

TEXT_A = (
    "Coffee contains caffeine, which blocks adenosine receptors in the brain. "
    "Moderate consumption is associated with improved alertness in most adults. "
)
TEXT_B = "Кофе вреден для сердца и повышает давление у большинства людей. " * 2


def _ctx(*texts: str, target: int = 400) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    versions: dict[str, Any] = {}
    for n, text in enumerate(texts):
        vid = f"v{n}"
        versions[vid] = {"source_id": f"s{n}", "title": f"Doc {n}", "kind": "text", "pages": None}
        for s in chunk_text(text, target):
            chunks.append(
                {
                    "key": f"c{len(chunks) + 1}",
                    "chunk_id": f"{vid}-{s.ordinal}",
                    "version_id": vid,
                    "ordinal": s.ordinal,
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                }
            )
    return chunks, versions


def _provider(llm: FakeLLMClient, **kw: Any) -> SourcesProvider:
    options = {
        "support_check": True,
        "summary_single_pass_chars": 10_000,
        "summary_batch_chars": 10_000,
        "summary_max_batches": 8,
    }
    options.update(kw)
    return SourcesProvider(llm, **options)


def _req(params: dict[str, Any]) -> GenerationRequest:
    return GenerationRequest(
        user_id=uuid.uuid4(),
        kind="answer",
        idempotency_key="k",
        params=params,
        model=None,
        request_id="r",
        deadline_s=10,
    )


def _answer(question: str, chunks: Any, versions: Any) -> GenerationRequest:
    return _req(
        {"op": "answer", "ref": "m", "question": question, "chunks": chunks, "versions": versions}
    )


async def test_answer_keeps_only_verified_citations_and_numbers_them() -> None:
    llm = FakeLLMClient()
    chunks, versions = _ctx(TEXT_A)
    llm.script(
        "answer",
        {
            "status": "answered",
            "claims": [
                {
                    "text": "Кофеин блокирует рецепторы аденозина.",
                    "citations": [
                        {"chunk": "c1", "quote": "caffeine, which blocks adenosine receptors"}
                    ],
                },
                {
                    "text": "Кофе лечит простуду.",
                    "citations": [{"chunk": "c1", "quote": "coffee cures the common cold"}],
                },
            ],
            "conflicts": [],
        },
    )
    result = await _provider(llm).generate(_answer("Как действует кофеин?", chunks, versions))
    out = result.output
    assert out["status"] == "answered"
    assert [b["text"] for b in out["blocks"]] == ["Кофеин блокирует рецепторы аденозина."]
    assert out["text"].endswith("[1]")
    citation = out["citations"][0]
    assert citation["quote"] == "caffeine, which blocks adenosine receptors"
    assert TEXT_A[citation["offsets"]["start"] : citation["offsets"]["end"]] == citation["quote"]
    assert citation["page"] is None  # text sources get no invented pages
    assert out["verification"]["droppedClaims"] == 1
    assert result.usage.units == 1 and result.usage.input_tokens > 0


async def test_unsupported_claim_is_dropped_and_nothing_left_means_no_answer() -> None:
    llm = FakeLLMClient()
    chunks, versions = _ctx(TEXT_A)
    llm.script(
        "answer",
        {
            "status": "answered",
            "claims": [
                {
                    "text": "Кофе безопасен для всех детей.",
                    "citations": [{"chunk": "c1", "quote": "Moderate consumption is associated"}],
                }
            ],
            "conflicts": [],
        },
    )
    llm.script("support_check", {"results": [{"id": "k1", "supported": False}]})
    out = (await _provider(llm).generate(_answer("Кофе детям?", chunks, versions))).output
    assert out["status"] == "no_answer" and out["text"] == NO_ANSWER_TEXT and out["citations"] == []


async def test_no_answer_when_materials_are_silent() -> None:
    chunks, versions = _ctx(TEXT_A)
    out = (
        await _provider(FakeLLMClient()).generate(
            _answer("Кто выиграл чемпионат мира по шахматам?", chunks, versions)
        )
    ).output
    assert out["status"] == "no_answer" and out["text"] == NO_ANSWER_TEXT


async def test_contradiction_shows_both_positions_with_their_sources() -> None:
    llm = FakeLLMClient()
    chunks, versions = _ctx(TEXT_A, TEXT_B)
    b_key = next(c["key"] for c in chunks if c["version_id"] == "v1")
    llm.script(
        "answer",
        {
            "status": "answered",
            "claims": [],
            "conflicts": [
                {
                    "description": "о пользе кофе",
                    "positions": [
                        {
                            "text": "Умеренное потребление связано с бодростью.",
                            "citations": [
                                {"chunk": "c1", "quote": "associated with improved alertness"}
                            ],
                        },
                        {
                            "text": "Кофе вреден для сердца.",
                            "citations": [
                                {
                                    "chunk": b_key,
                                    "quote": "Кофе вреден для сердца и повышает давление",
                                }
                            ],
                        },
                    ],
                }
            ],
        },
    )
    out = (await _provider(llm).generate(_answer("Кофе полезен?", chunks, versions))).output
    block = out["blocks"][0]
    assert block["type"] == "conflict" and len(block["positions"]) == 2
    assert {c["sourceId"] for c in out["citations"]} == {"s0", "s1"}
    assert "Источники расходятся" in out["text"]


async def test_llm_failure_is_a_provider_error_so_the_core_charges_nothing() -> None:
    llm = FakeLLMClient()
    llm.script("answer", LLMError("llm_unavailable", retryable=True))
    chunks, versions = _ctx(TEXT_A)
    with pytest.raises(ProviderError) as exc:
        await _provider(llm).generate(_answer("?", chunks, versions))
    assert exc.value.retryable is True


async def test_cancel_between_calls_raises_provider_error() -> None:
    async def canceled(op: str, ref: str) -> bool:
        return True

    chunks, versions = _ctx(TEXT_A)
    with pytest.raises(ProviderError) as exc:
        await _provider(FakeLLMClient(), cancel_check=canceled).generate(
            _answer("caffeine", chunks, versions)
        )
    assert exc.value.code == "canceled"


async def test_summary_map_reduce_reports_partial_coverage() -> None:
    text = "".join(
        f"Section {i} explains a distinct important idea about topic number {i} in detail. " * 3
        + "\n\n"
        for i in range(12)
    )
    chunks, versions = _ctx(text, target=300)
    llm = FakeLLMClient()
    provider = _provider(
        llm, summary_single_pass_chars=500, summary_batch_chars=600, summary_max_batches=3
    )
    out = (
        await provider.generate(
            _req({"op": "summary", "ref": "s", "chunks": chunks, "versions": versions})
        )
    ).output
    tasks = [c["task"] for c in llm.calls]
    assert tasks.count("summary_map") == 3 and tasks[-1] == "summary_reduce"
    assert out["theses"] and all(t["citations"] for t in out["theses"])
    cov = out["coverage"]
    assert cov["partial"] is True and 0 < cov["coveredChars"] < cov["totalChars"]
    for citation in out["citations"]:
        assert text[citation["offsets"]["start"] : citation["offsets"]["end"]] == citation["quote"]


def _fail_upstream() -> None:
    _contract_llm.script("answer", LLMError("llm_unavailable", retryable=True))


_contract_llm = FakeLLMClient()


class _ContractProvider(SourcesProvider):
    """The core contract-suite sends ``params={"prompt": ...}``; map it to a real answer call."""

    async def generate(self, req: GenerationRequest) -> Any:
        chunks, versions = _ctx(TEXT_A)
        params = {
            "op": "answer",
            "ref": "c",
            "question": "caffeine adenosine",
            "chunks": chunks,
            "versions": versions,
        }
        return await super().generate(GenerationRequest(**{**req.__dict__, "params": params}))


TestSourcesProviderContract = provider_contract_suite(
    _ContractProvider(
        _contract_llm,
        support_check=False,
        summary_single_pass_chars=1000,
        summary_batch_chars=1000,
        summary_max_batches=2,
    ),
    kind="answer",
    fail_upstream=_fail_upstream,
)
