"""LLM access: one narrow interface, two implementations.

``complete_json`` is the ONLY thing the pipeline asks of a model: given a system prompt, user
content blocks and a JSON schema, return a dict matching the schema. Everything that makes the
product trustworthy (quote verification, offsets, pages, coverage) happens in OUR code on top of
that dict — the model is never trusted to be right about where a quote is.

* ``OpenAILLMClient`` — OpenAI Responses API: strict structured outputs, a model per task,
  ``store=False``, backup-key and proxy failover.
* ``FakeLLMClient`` — deterministic, offline. Quotes real sentences from the given context, so
  local runs and the test-suite exercise the full citation pipeline without network or keys.

Keys stay on the server; prompts and model outputs are never logged (only token counts).
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Protocol

from app.observability.logging import log_event

logger = logging.getLogger("app.domain.llm")


class LLMError(Exception):
    def __init__(self, code: str, message: str | None = None, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message or code)


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class LLMClient(Protocol):
    name: str

    async def complete_json(
        self,
        *,
        task: str,
        system: str,
        content: list[dict[str, Any]],
        schema: dict[str, Any],
        fake_context: dict[str, Any] | None = None,
    ) -> LLMResult: ...


class OpenAILLMClient:
    """OpenAI Responses API with strict structured outputs (``text.format`` json_schema).

    * a model per task: answers/summaries on the main model, the support check on a cheap one;
    * ``store=False`` — user documents and answers are not kept in the provider's response store;
    * key failover: primary → backup key on auth/quota errors (as in claude-ios, ADR-074);
    * proxy failover: ``OPENAI_PROXY_URLS`` tried in order on CONNECTION errors only (as in
      232-claude-backend, ADR-010: a direct call from RU answers 403). An HTTP error returned by
      OpenAI itself is never re-sent through another proxy — that could pay twice.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_keys: list[str],
        base_url: str,
        proxy_urls: list[str],
        models: dict[str, str],
        reasoning: dict[str, str],
        max_output_tokens: int,
        timeout: float,
        max_retries: int,
    ) -> None:
        import openai

        self._openai = openai
        self._models = models
        self._reasoning = reasoning
        self._max_output_tokens = max_output_tokens
        self._clients: list[Any] = []
        for key in [k for k in api_keys if k] or [""]:
            proxies: list[str | None] = list(proxy_urls) or [None]
            for proxy in proxies:
                http_client = (
                    openai.DefaultAsyncHttpxClient(proxy=proxy) if proxy is not None else None
                )
                self._clients.append(
                    openai.AsyncOpenAI(
                        api_key=key or "missing",
                        base_url=base_url or None,
                        timeout=timeout,
                        max_retries=max_retries,
                        http_client=http_client,
                    )
                )
        self._per_key = max(len(proxy_urls), 1)

    def model_for(self, task: str) -> str:
        return self._models.get(task) or self._models["default"]

    def _reasoning_param(self, task: str, model: str) -> dict[str, str] | None:
        effort = self._reasoning.get(task) or self._reasoning.get("default")
        # Only reasoning models accept `reasoning.effort` (gpt-5*, o-series).
        if effort and (model.startswith("gpt-5") or model[:2] in ("o1", "o3", "o4")):
            return {"effort": effort}
        return None

    def _next_key(self, index: int) -> int:
        return (index // self._per_key + 1) * self._per_key

    async def complete_json(
        self,
        *,
        task: str,
        system: str,
        content: list[dict[str, Any]],
        schema: dict[str, Any],
        fake_context: dict[str, Any] | None = None,
    ) -> LLMResult:
        openai = self._openai
        model = self.model_for(task)
        kwargs: dict[str, Any] = {
            "model": model,
            # Frozen instructions, then the materials, then the volatile question: the longest
            # stable prefix for OpenAI's automatic prompt caching.
            "instructions": system,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": b["text"]} for b in content],
                }
            ],
            "text": {
                "format": {"type": "json_schema", "name": task, "schema": schema, "strict": True}
            },
            "max_output_tokens": self._max_output_tokens,
            "store": False,
            "prompt_cache_key": f"docs-rag:{task}",
        }
        reasoning = self._reasoning_param(task, model)
        if reasoning:
            kwargs["reasoning"] = reasoning

        response: Any = None
        last_error: LLMError | None = None
        index = 0
        while index < len(self._clients):
            try:
                response = await self._clients[index].responses.create(**kwargs)
                break
            except openai.APIConnectionError as exc:  # includes timeouts → next proxy
                last_error = LLMError("llm_unavailable", retryable=True)
                last_error.__cause__ = exc
                index += 1
            except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
                last_error = LLMError("llm_auth_failed")
                last_error.__cause__ = exc
                index = self._next_key(index)
            except openai.RateLimitError as exc:
                if getattr(exc, "code", None) != "insufficient_quota":
                    raise LLMError("llm_rate_limited", retryable=True) from exc
                last_error = LLMError("llm_quota_exhausted")
                last_error.__cause__ = exc
                index = self._next_key(index)
            except openai.BadRequestError as exc:
                raise LLMError("llm_bad_request") from exc
            except openai.APIStatusError as exc:
                raise LLMError(
                    f"llm_http_{exc.status_code}", retryable=exc.status_code >= 500
                ) from exc
        if response is None:
            raise last_error or LLMError("llm_unavailable", retryable=True)

        usage = response.usage
        input_tokens = int(usage.input_tokens) if usage else 0
        details = getattr(usage, "input_tokens_details", None) if usage else None
        cached = int(getattr(details, "cached_tokens", 0) or 0)
        output_tokens = int(usage.output_tokens) if usage else 0
        log_event(
            logger,
            logging.INFO,
            "llm_call",
            task=task,
            model=model,
            status=response.status,
            inputTokens=input_tokens,
            cachedTokens=cached,
            outputTokens=output_tokens,
        )
        for item in response.output or []:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    raise LLMError("llm_refusal")
        if response.status == "incomplete":
            raise LLMError("llm_output_truncated")
        text = response.output_text
        if not text:
            raise LLMError("llm_empty_output")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("llm_invalid_json") from exc
        if not isinstance(data, dict):
            raise LLMError("llm_invalid_json")
        return LLMResult(
            data=data,
            # The requested alias (gpt-5.1), not a dated snapshot: price tables key on the alias.
            model=model,
            # OpenAI's input_tokens INCLUDE the cached prefix; the uncached part is reported
            # separately so a cost calculation never bills the cached prefix twice.
            input_tokens=input_tokens - cached,
            output_tokens=output_tokens,
            cache_read_tokens=cached,
            cache_write_tokens=0,
        )


# --- the deterministic offline model -----------------------------------------------------------
_SENTENCE = re.compile(r"[^.!?…\n]{20,400}[.!?…]")
_WORD = re.compile(r"\w{4,}", re.UNICODE)


def _sentences(text: str) -> list[str]:
    found = [m.group(0).strip() for m in _SENTENCE.finditer(text)]
    return [s for s in found if len(s) >= 20]


def _stems(text: str) -> set[str]:
    return {w.lower()[:5] for w in _WORD.findall(text)}


class FakeLLMClient:
    """Deterministic stand-in. ``script(task, data)`` queues an exact response for a task."""

    name = "fake"

    def __init__(self) -> None:
        self._scripted: dict[str, deque[dict[str, Any] | Exception]] = defaultdict(deque)
        self.calls: list[dict[str, Any]] = []

    def script(self, task: str, response: dict[str, Any] | Exception) -> None:
        self._scripted[task].append(response)

    async def complete_json(
        self,
        *,
        task: str,
        system: str,
        content: list[dict[str, Any]],
        schema: dict[str, Any],
        fake_context: dict[str, Any] | None = None,
    ) -> LLMResult:
        ctx = fake_context or {}
        self.calls.append({"task": task, "system": system, "content": content, "context": ctx})
        if self._scripted[task]:
            scripted = self._scripted[task].popleft()
            if isinstance(scripted, Exception):
                raise scripted
            return LLMResult(data=scripted, model="fake", input_tokens=100, output_tokens=50)
        handler = getattr(self, f"_{task}", None)
        if handler is None:
            raise LLMError("unknown_task")
        data = handler(ctx)
        size = sum(len(json.dumps(block, ensure_ascii=False)) for block in content)
        return LLMResult(data=data, model="fake", input_tokens=size // 3, output_tokens=200)

    @staticmethod
    def _answer(ctx: dict[str, Any]) -> dict[str, Any]:
        wanted = _stems(ctx.get("question", ""))
        scored: list[tuple[int, str, str]] = []
        for chunk in ctx.get("chunks", []):
            for sentence in _sentences(chunk["text"]):
                score = len(wanted & _stems(sentence))
                if score:
                    scored.append((score, chunk["key"], sentence))
        if not scored:
            return {"status": "no_answer", "claims": [], "conflicts": []}
        scored.sort(key=lambda item: -item[0])
        claims = [
            {
                "text": f"В материалах сказано: {sentence}",
                "citations": [{"chunk": key, "quote": sentence}],
            }
            for _, key, sentence in scored[:2]
        ]
        return {"status": "answered", "claims": claims, "conflicts": []}

    @staticmethod
    def _summary_map(ctx: dict[str, Any]) -> dict[str, Any]:
        chunks = ctx.get("chunks", [])
        step = max(len(chunks) // 6, 1)
        theses = []
        for chunk in chunks[::step][:6]:
            sentences = _sentences(chunk["text"])
            if sentences:
                theses.append(
                    {
                        "text": f"Тезис: {sentences[0]}",
                        "citations": [{"chunk": chunk["key"], "quote": sentences[0]}],
                    }
                )
        return {
            "theses": theses,
            "questions": [
                "Какая главная мысль материалов?",
                "Какие аргументы приводятся?",
                "Какие выводы делают авторы?",
            ],
        }

    @staticmethod
    def _summary_reduce(ctx: dict[str, Any]) -> dict[str, Any]:
        items = ctx.get("theses", [])[:7]
        return {
            "theses": [{"text": t["text"], "based_on": [t["id"]]} for t in items],
            "questions": ["Какая главная мысль материалов?", "Что следует из выводов?"],
        }

    @staticmethod
    def _support_check(ctx: dict[str, Any]) -> dict[str, Any]:
        return {"results": [{"id": c["id"], "supported": True} for c in ctx.get("claims", [])]}


def build_llm_client(settings: Any) -> LLMClient:
    if settings.llm_provider == "fake":
        return FakeLLMClient()
    main = settings.openai_model
    return OpenAILLMClient(
        api_keys=[settings.openai_api_key, settings.openai_api_key_backup],
        base_url=settings.openai_base_url,
        proxy_urls=[u.strip() for u in settings.openai_proxy_urls.split(",") if u.strip()],
        models={
            "default": main,
            "answer": main,
            "summary_map": settings.openai_summary_model or main,
            "summary_reduce": settings.openai_summary_model or main,
            "support_check": settings.openai_check_model or main,
        },
        reasoning={
            "default": settings.openai_reasoning_effort,
            "support_check": settings.openai_check_reasoning_effort,
        },
        max_output_tokens=settings.openai_max_output_tokens,
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )
