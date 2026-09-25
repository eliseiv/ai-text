"""OpenAI client behaviour (with a fake SDK boundary) and the volume-based summary price."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.domain.llm import LLMError, OpenAILLMClient
from app.domain.pricing import DomainPricing, summary_parts, summary_price
from app.domain.prompts import ANSWER_SCHEMA
from app.generation.contract import GenerationUsage

_REQ = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status(cls: type[openai.APIStatusError], code: int, err_code: str | None = None) -> Exception:
    body = {"code": err_code} if err_code else None  # the SDK passes the inner `error` object
    exc = cls("boom", response=httpx.Response(code, request=_REQ), body=body)
    return exc


def _response(data: dict[str, Any] | None = None, **kw: Any) -> Any:
    text = json.dumps(data or {"status": "no_answer", "claims": [], "conflicts": []})
    part = kw.pop("part", SimpleNamespace(type="output_text", text=text))
    return SimpleNamespace(
        model="gpt-5.1-2025-11-13",
        status=kw.pop("status", "completed"),
        output=[SimpleNamespace(type="message", content=[part])],
        output_text=text if part.type == "output_text" else "",
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=200,
            input_tokens_details=SimpleNamespace(cached_tokens=800),
        ),
    )


class _FakeResponses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(
    *, keys: int = 1, proxies: int = 0, outcomes: list[list[Any]]
) -> tuple[OpenAILLMClient, list[_FakeResponses]]:
    client = OpenAILLMClient(
        api_keys=[f"key-{i}" for i in range(keys)],
        base_url="",
        proxy_urls=[f"http://proxy-{i}.example:8080" for i in range(proxies)],
        models={"default": "gpt-5.1", "support_check": "gpt-5-mini", "answer": "gpt-5.1"},
        reasoning={"default": "low", "support_check": "minimal"},
        max_output_tokens=4000,
        timeout=5,
        max_retries=0,
    )
    fakes = [_FakeResponses(list(o)) for o in outcomes]
    assert len(fakes) == len(client._clients)
    client._clients = [SimpleNamespace(responses=f) for f in fakes]
    return client, fakes


async def _call(client: OpenAILLMClient, task: str = "answer") -> Any:
    return await client.complete_json(
        task=task,
        system="SYSTEM",
        content=[
            {"type": "text", "text": "materials", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "question"},
        ],
        schema=ANSWER_SCHEMA,
    )


async def test_request_shape_and_token_accounting() -> None:
    client, (fake,) = _client(outcomes=[[_response()]])
    result = await _call(client)
    sent = fake.calls[0]
    assert sent["model"] == "gpt-5.1" and sent["instructions"] == "SYSTEM"
    assert sent["store"] is False  # documents are not kept in the provider's response store
    assert sent["text"]["format"] == {
        "type": "json_schema",
        "name": "answer",
        "schema": ANSWER_SCHEMA,
        "strict": True,
    }
    assert [p["text"] for p in sent["input"][0]["content"]] == ["materials", "question"]
    assert sent["reasoning"] == {"effort": "low"}
    assert result.model == "gpt-5.1"  # the alias, not the dated snapshot
    assert (result.input_tokens, result.cache_read_tokens, result.output_tokens) == (200, 800, 200)


async def test_support_check_uses_the_small_model() -> None:
    client, (fake,) = _client(outcomes=[[_response({"results": []})]])
    await _call(client, task="support_check")
    assert fake.calls[0]["model"] == "gpt-5-mini"
    assert fake.calls[0]["reasoning"] == {"effort": "minimal"}


async def test_backup_key_takes_over_on_auth_and_quota_errors() -> None:
    client, fakes = _client(
        keys=2, outcomes=[[_status(openai.AuthenticationError, 401)], [_response()]]
    )
    await _call(client)
    assert len(fakes[1].calls) == 1

    client, fakes = _client(
        keys=2,
        outcomes=[[_status(openai.RateLimitError, 429, "insufficient_quota")], [_response()]],
    )
    await _call(client)
    assert len(fakes[1].calls) == 1


async def test_plain_rate_limit_is_retryable_and_not_failed_over() -> None:
    client, fakes = _client(keys=2, outcomes=[[_status(openai.RateLimitError, 429)], []])
    with pytest.raises(LLMError) as exc:
        await _call(client)
    assert exc.value.retryable is True and fakes[1].calls == []


async def test_connection_errors_walk_the_proxies_then_give_up_retryable() -> None:
    conn = openai.APIConnectionError(request=_REQ)
    client, fakes = _client(proxies=2, outcomes=[[conn], [_response()]])
    await _call(client)
    assert len(fakes[1].calls) == 1

    client, _ = _client(proxies=2, outcomes=[[conn], [openai.APIConnectionError(request=_REQ)]])
    with pytest.raises(LLMError) as exc:
        await _call(client)
    assert exc.value.code == "llm_unavailable" and exc.value.retryable


async def test_server_error_is_not_resent_through_another_proxy() -> None:
    client, fakes = _client(proxies=2, outcomes=[[_status(openai.InternalServerError, 500)], []])
    with pytest.raises(LLMError) as exc:
        await _call(client)
    assert exc.value.retryable and fakes[1].calls == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_response(part=SimpleNamespace(type="refusal", refusal="no")), "llm_refusal"),
        (_response(status="incomplete"), "llm_output_truncated"),
    ],
)
async def test_refusal_and_truncation_are_failures(response: Any, code: str) -> None:
    client, _ = _client(outcomes=[[response]])
    with pytest.raises(LLMError) as exc:
        await _call(client)
    assert exc.value.code == code


# --- pricing -----------------------------------------------------------------------------------
def test_summary_price_grows_with_volume_and_is_capped_by_parts() -> None:
    small = [1500] * 10  # 15k chars: one pass
    large = [1500] * 200  # 300k chars: several parts
    huge = [1500] * 2000  # 3M chars: more than SUMMARY_MAX_BATCHES parts
    assert summary_parts(small) == 1 and summary_price(1) == 3
    parts = summary_parts(large)
    assert parts == 3 and summary_price(parts) == 3 + 2 * 2
    assert summary_parts(huge) == 8


def test_quote_equals_charge() -> None:
    pricing = DomainPricing()
    chunks = [{"text": "x" * 1500}] * 200
    quoted = pricing.quote(kind="summary", model=None, params={"chunks": chunks})
    charged = pricing.charge(
        kind="summary",
        model=None,
        usage=GenerationUsage(model="m", units=summary_parts([1500] * 200)),
    )
    assert quoted == charged == 7
    assert pricing.quote(kind="answer", model=None, params={}) == 1


def test_empty_base_url_env_does_not_break_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """`OPENAI_BASE_URL=` (empty) in .env must not become base_url="" (UnsupportedProtocol)."""
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    client = OpenAILLMClient(
        api_keys=["k"],
        base_url="",
        proxy_urls=[],
        models={"default": "gpt-5.1"},
        reasoning={},
        max_output_tokens=10,
        timeout=5,
        max_retries=0,
    )
    assert str(client._clients[0].base_url).rstrip("/") == "https://api.openai.com/v1"
