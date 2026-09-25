"""Price of an operation in credits (rationale and unit economics: ``docs/pricing.md``).

* answer — ``ANSWER_CREDITS`` (fixed: a question costs the same whatever the model writes);
* summary — ``SUMMARY_CREDITS`` for a topic that fits one pass, plus
  ``SUMMARY_CREDITS_PER_EXTRA_PART`` for every extra part of a long topic.

The number of summary parts comes from ``plan_summary_parts`` — the SAME function the provider
uses to split the work — so ``quote()`` (before the run, from the chunk sizes) equals
``charge()`` (after it, from ``usage.units`` = parts actually read). Never from the client body,
never from the model output. Other kinds (the core's dev harness) use the built-in policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.config import get_settings
from app.generation.contract import GenerationUsage
from app.generation.pricing import build_pricing

KIND_ANSWER = "answer"
KIND_SUMMARY = "summary"


def _cap(value: int, settings: Any) -> int:
    return max(0, min(int(value), int(settings.pricing_max_credits_per_generation)))


def summary_parts(chunk_lengths: Sequence[int], settings: Any | None = None) -> int:
    from app.domain.provider import plan_summary_parts

    s: Any = settings or get_settings()
    plan = plan_summary_parts(
        chunk_lengths,
        single_pass=s.summary_single_pass_chars,
        part_chars=s.summary_batch_chars,
    )
    return max(1, min(len(plan), int(s.summary_max_batches)))


def summary_price(parts: int, settings: Any | None = None) -> int:
    s: Any = settings or get_settings()
    extra = max(parts - 1, 0) * int(s.summary_credits_per_extra_part)
    return _cap(int(s.summary_credits) + extra, s)


def answer_price(settings: Any | None = None) -> int:
    s: Any = settings or get_settings()
    return _cap(int(s.answer_credits), s)


class DomainPricing:
    def quote(self, *, kind: str, model: str | None, params: dict[str, Any]) -> int:
        if kind == KIND_ANSWER:
            return answer_price()
        if kind == KIND_SUMMARY:
            chunks = params.get("chunks") or []
            return summary_price(summary_parts([len(c["text"]) for c in chunks]))
        return build_pricing(get_settings()).quote(kind=kind, model=model, params=params)

    def charge(self, *, kind: str, model: str | None, usage: GenerationUsage) -> int:
        if kind == KIND_ANSWER:
            return answer_price()
        if kind == KIND_SUMMARY:
            return summary_price(max(int(usage.units), 1))
        return build_pricing(get_settings()).charge(kind=kind, model=model, usage=usage)
