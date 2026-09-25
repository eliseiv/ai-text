"""``SourcesProvider`` — the domain's ``GenerationProvider``: answers and summaries.

Plugged into the core ``GenerationService.run()``, so policy, the idempotency anchor, "no charge on
failure" (``ProviderError`` ⇒ 0 credits), the debit, accounting and audit come from the core.

It stays a PURE function of its ``params`` (the exact context assembled by the worker from the
user's own topic): no DB, no wallet. The only side channel is ``cancel_check`` — a read of the
job's status between LLM calls, so a canceled request stops, raises ``ProviderError`` and is
therefore NOT charged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.citations import (
    CitationResolver,
    CitationTable,
    VerificationStats,
    VerifiedClaim,
    markers,
)
from app.domain.llm import LLMClient, LLMError, LLMResult
from app.domain.prompts import (
    ANSWER_SCHEMA,
    ANSWER_SYSTEM,
    NO_ANSWER_TEXT,
    SUMMARY_MAP_SCHEMA,
    SUMMARY_MAP_SYSTEM,
    SUMMARY_REDUCE_SCHEMA,
    SUMMARY_REDUCE_SYSTEM,
    SUPPORT_SCHEMA,
    SUPPORT_SYSTEM,
    PromptChunk,
    materials_block,
    question_block,
)
from app.generation.contract import (
    GenerationRequest,
    GenerationResult,
    GenerationStatus,
    GenerationUsage,
    ProviderError,
    ProviderHealth,
)

PROVIDER_NAME = "sources-rag"
CancelCheck = Callable[[str, str], Awaitable[bool]]


async def _never_canceled(_op: str, _ref: str) -> bool:
    return False


@dataclass
class _Usage:
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0

    def add(self, result: LLMResult) -> None:
        self.model = result.model
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.cache_read += result.cache_read_tokens
        self.cache_write += result.cache_write_tokens
        self.calls += 1


@dataclass
class _Run:
    op: str
    ref: str
    usage: _Usage = field(default_factory=_Usage)
    units: int = 1


def plan_summary_parts(
    lengths: Sequence[int], *, single_pass: int, part_chars: int
) -> list[list[int]]:
    """How a summary splits its chunks (by index) into model calls.

    ONE function for both the provider (what it reads) and the pricing (what it charges): the price
    quoted before the run is exactly the price of what the run does.
    """
    if sum(lengths) <= single_pass:
        return [list(range(len(lengths)))] if lengths else []
    parts: list[list[int]] = [[]]
    size = 0
    for i, length in enumerate(lengths):
        if parts[-1] and size + length > part_chars:
            parts.append([])
            size = 0
        parts[-1].append(i)
        size += length
    return [p for p in parts if p]


def _prompt_chunks(
    chunks: Sequence[Mapping[str, Any]], versions: Mapping[str, Mapping[str, Any]]
) -> list[PromptChunk]:
    from app.domain.text import page_at

    result = []
    for chunk in chunks:
        version = versions[chunk["version_id"]]
        page = page_at(version.get("pages"), chunk["start"])
        page_label = (page.label or str(page.physical)) if page else None
        result.append(
            PromptChunk(
                key=chunk["key"],
                source_title=str(version.get("title", "")),
                source_kind=str(version.get("kind", "")),
                page_label=page_label,
                text=chunk["text"],
            )
        )
    return result


class SourcesProvider:
    name = PROVIDER_NAME
    kind = "answer"

    def __init__(
        self,
        llm: LLMClient,
        *,
        support_check: bool,
        summary_single_pass_chars: int,
        summary_batch_chars: int,
        summary_max_batches: int,
        cancel_check: CancelCheck = _never_canceled,
    ) -> None:
        self._llm = llm
        self._support_check = support_check
        self._single_pass = summary_single_pass_chars
        self._batch_chars = summary_batch_chars
        self._max_batches = summary_max_batches
        self._cancel_check = cancel_check

    # ------------------------------------------------------------------ contract
    async def generate(self, req: GenerationRequest) -> GenerationResult:
        op = str(req.params.get("op", ""))
        run = _Run(op=op, ref=str(req.params.get("ref", "")))
        try:
            async with asyncio.timeout(req.deadline_s):
                if op == "answer":
                    output = await self._answer(run, req.params)
                elif op == "summary":
                    output = await self._summary(run, req.params)
                else:
                    raise ProviderError("unknown_operation")
        except TimeoutError as exc:
            raise ProviderError("timeout", retryable=True) from exc
        except LLMError as exc:
            raise ProviderError(
                exc.code, provider_error_type=exc.code, retryable=exc.retryable
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed model output (or context) is a failed generation: 0 credits.
            raise ProviderError("invalid_output", message=type(exc).__name__) from exc
        return GenerationResult(
            status=GenerationStatus.succeeded,
            output=output,
            usage=GenerationUsage(
                model=run.usage.model or "unknown",
                input_tokens=run.usage.input_tokens,
                output_tokens=run.usage.output_tokens,
                cache_read_tokens=run.usage.cache_read,
                cache_write_tokens=run.usage.cache_write,
                # Answers: 1. Summaries: the number of parts read — the price depends on it.
                units=run.units,
                unit_kind=op,
            ),
            stop_reason="end",
        )

    async def poll(self, provider_ref: str) -> GenerationResult:
        raise NotImplementedError("SourcesProvider is synchronous (the worker makes it async)")

    async def healthcheck(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail=f"llm={getattr(self._llm, 'name', '?')}")

    # ------------------------------------------------------------------ helpers
    async def _call(
        self,
        run: _Run,
        *,
        task: str,
        system: str,
        content: list[dict[str, Any]],
        schema: dict[str, Any],
        fake_context: dict[str, Any],
    ) -> dict[str, Any]:
        if await self._cancel_check(run.op, run.ref):
            raise ProviderError("canceled")
        result = await self._llm.complete_json(
            task=task, system=system, content=content, schema=schema, fake_context=fake_context
        )
        run.usage.add(result)
        if await self._cancel_check(run.op, run.ref):
            raise ProviderError("canceled")
        return result.data

    async def _filter_supported(
        self, run: _Run, claims: list[VerifiedClaim]
    ) -> list[VerifiedClaim]:
        """Second pass: keep only claims that their quotes actually support."""
        if not self._support_check or not claims:
            return claims
        items = [
            {"id": f"k{i + 1}", "text": c.text, "quotes": [x.quote for x in c.citations]}
            for i, c in enumerate(claims)
        ]
        rendered = (
            "<claims>\n"
            + "\n".join(
                f'<claim id="{it["id"]}">\nУтверждение: {it["text"]}\nЦитаты:\n'
                + "\n".join(f"- «{q}»" for q in it["quotes"])
                + "\n</claim>"
                for it in items
            )
            + "\n</claims>"
        )
        data = await self._call(
            run,
            task="support_check",
            system=SUPPORT_SYSTEM,
            content=[{"type": "text", "text": rendered}],
            schema=SUPPORT_SCHEMA,
            fake_context={"claims": items},
        )
        verdict = {str(r["id"]): bool(r["supported"]) for r in data.get("results", [])}
        # Fail-closed: a claim the checker did not rule on is dropped, not assumed supported.
        return [c for it, c in zip(items, claims, strict=True) if verdict.get(str(it["id"]), False)]

    # ------------------------------------------------------------------ answer
    async def _answer(self, run: _Run, params: Mapping[str, Any]) -> dict[str, Any]:
        chunks = params["chunks"]
        versions = params["versions"]
        question = str(params["question"])
        history = [(str(q), str(a)) for q, a in params.get("history", [])]
        data = await self._call(
            run,
            task="answer",
            system=ANSWER_SYSTEM,
            content=[
                materials_block(_prompt_chunks(chunks, versions)),
                question_block(question, history),
            ],
            schema=ANSWER_SCHEMA,
            fake_context={"question": question, "chunks": chunks},
        )
        resolver = CitationResolver(chunks, versions)
        stats = VerificationStats()
        claims: list[VerifiedClaim] = []
        conflicts: list[tuple[str, list[VerifiedClaim]]] = []
        if data.get("status") != "no_answer":
            claims = resolver.verify_claims(data.get("claims", []), stats)
            for raw in data.get("conflicts", []):
                positions = resolver.verify_claims(raw.get("positions", []), stats)
                if len(positions) >= 2:
                    conflicts.append((str(raw.get("description", "")).strip(), positions))
                else:
                    claims.extend(positions)  # a "conflict" with one sourced side is a claim

        if self._support_check and (claims or conflicts):
            flat = claims + [p for _, ps in conflicts for p in ps]
            supported = {id(c) for c in await self._filter_supported(run, flat)}
            stats.dropped_claims += sum(1 for c in flat if id(c) not in supported)
            claims = [c for c in claims if id(c) in supported]
            kept_conflicts = []
            for description, positions in conflicts:
                positions = [p for p in positions if id(p) in supported]
                if len(positions) >= 2:
                    kept_conflicts.append((description, positions))
                else:
                    claims.extend(positions)
            conflicts = kept_conflicts

        table = CitationTable()
        blocks: list[dict[str, Any]] = []
        paragraphs: list[str] = []
        for claim in claims:
            idx = [table.add(c) for c in claim.citations]
            blocks.append({"type": "claim", "text": claim.text, "citations": [i + 1 for i in idx]})
            paragraphs.append(f"{claim.text} {markers(idx)}")
        for description, positions in conflicts:
            block_positions = []
            lines = [
                f"Источники расходятся: {description}" if description else "Источники расходятся:"
            ]
            for position in positions:
                idx = [table.add(c) for c in position.citations]
                block_positions.append({"text": position.text, "citations": [i + 1 for i in idx]})
                lines.append(f"— {position.text} {markers(idx)}")
            blocks.append({"type": "conflict", "text": description, "positions": block_positions})
            paragraphs.append("\n".join(lines))

        answered = bool(blocks)
        return {
            "status": "answered" if answered else "no_answer",
            "text": "\n\n".join(paragraphs) if answered else NO_ANSWER_TEXT,
            "blocks": blocks,
            "citations": table.to_json(),
            "partialContext": bool(params.get("partialContext", False)),
            "verification": stats.to_json(),
        }

    # ------------------------------------------------------------------ summary
    async def _summary(self, run: _Run, params: Mapping[str, Any]) -> dict[str, Any]:
        chunks: list[Mapping[str, Any]] = list(params["chunks"])
        versions = params["versions"]
        total_chars = sum(len(c["text"]) for c in chunks)
        stats = VerificationStats()
        resolver = CitationResolver(chunks, versions)

        plan = plan_summary_parts(
            [len(c["text"]) for c in chunks],
            single_pass=self._single_pass,
            part_chars=self._batch_chars,
        )
        read = [[chunks[i] for i in part] for part in plan[: self._max_batches]]
        run.units = len(read)

        map_results: list[tuple[list[VerifiedClaim], list[str]]] = []
        for batch in read:
            data = await self._call(
                run,
                task="summary_map",
                system=SUMMARY_MAP_SYSTEM,
                content=[
                    materials_block(_prompt_chunks(batch, versions)),
                    {"type": "text", "text": "Составь конспект этих материалов."},
                ],
                schema=SUMMARY_MAP_SCHEMA,
                fake_context={"chunks": batch},
            )
            theses = resolver.verify_claims(data.get("theses", []), stats)
            map_results.append((theses, [str(q) for q in data.get("questions", [])]))

        table = CitationTable()
        final: list[dict[str, Any]] = []
        questions: list[str] = []
        if len(map_results) == 1:
            theses, questions = map_results[0]
            for thesis in theses:
                idx = [table.add(c) for c in thesis.citations]
                final.append({"text": thesis.text, "citations": [i + 1 for i in idx]})
        else:
            partial: dict[str, VerifiedClaim] = {}
            for theses, qs in map_results:
                for thesis in theses:
                    partial[f"t{len(partial) + 1}"] = thesis
                questions.extend(qs)
            listed = [{"id": k, "text": v.text} for k, v in partial.items()]
            rendered = (
                "<theses>\n" + "\n".join(f'[{t["id"]}] {t["text"]}' for t in listed) + "\n</theses>"
            )
            data = await self._call(
                run,
                task="summary_reduce",
                system=SUMMARY_REDUCE_SYSTEM,
                content=[{"type": "text", "text": rendered}],
                schema=SUMMARY_REDUCE_SCHEMA,
                fake_context={"theses": listed},
            )
            questions = [str(q) for q in data.get("questions", [])] or questions
            for merged in data.get("theses", []):
                cited = []
                for ref in merged.get("based_on", []):
                    source = partial.get(str(ref))
                    if source is not None:
                        cited.extend(source.citations)
                if not cited:
                    stats.dropped_claims += 1
                    continue  # a merged thesis that points at nothing verifiable is dropped
                merged_idx: list[int] = []
                for citation in cited:
                    i = table.add(citation)
                    if i not in merged_idx:
                        merged_idx.append(i)
                final.append(
                    {
                        "text": str(merged["text"]).strip(),
                        "citations": [i + 1 for i in merged_idx[:3]],
                    }
                )

        covered: dict[str, int] = {}
        for batch in read:
            for chunk in batch:
                covered[chunk["version_id"]] = covered.get(chunk["version_id"], 0) + len(
                    chunk["text"]
                )
        per_source = []
        for version_id, version in versions.items():
            total = sum(len(c["text"]) for c in chunks if c["version_id"] == version_id)
            per_source.append(
                {
                    "sourceId": str(version["source_id"]),
                    "versionId": version_id,
                    "title": version.get("title"),
                    "totalChars": total,
                    "coveredChars": covered.get(version_id, 0),
                }
            )
        covered_chars = sum(covered.values())
        seen_q: set[str] = set()
        clean_questions = []
        for q in questions:
            q = q.strip()
            if q and q.lower() not in seen_q:
                seen_q.add(q.lower())
                clean_questions.append(q)
        return {
            "theses": final,
            "questions": clean_questions[:5],
            "citations": table.to_json(),
            "coverage": {
                "totalChars": total_chars,
                "coveredChars": covered_chars,
                "partial": covered_chars < total_chars,
                "sources": per_source,
            },
            "verification": stats.to_json(),
        }
