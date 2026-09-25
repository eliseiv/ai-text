"""Server-side citation verification — the client never builds a citation itself.

Input: the model's claims ``{text, citations: [{chunk, quote}]}`` + the exact context the model
was given. Output: claims whose every kept citation is a REAL fragment of a selected source
version, with version offsets and (for PDF) physical + printed page.

Rules:
* a citation naming a chunk key that was not in the context is dropped (the model cannot point
  outside the current topic/selection — prompt-injection containment);
* the quote must be found in the text (see ``text.find_quote``); it is searched first in the run of
  consecutive chunks around the named chunk (quotes may cross a chunk boundary), then in the rest
  of the context (the model sometimes names the neighbouring chunk);
* the stored quote is the ORIGINAL text slice, not the model's rendering of it;
* a claim left without citations is dropped: «каждый существенный тезис связан с цитатой».
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.text import find_quote, page_at

MAX_CITATIONS_PER_CLAIM = 3


@dataclass
class _Run:
    version_id: str
    start: int  # version offset of text[0]
    text: str
    chunk_keys: set[str]


@dataclass
class Resolved:
    source_id: str
    version_id: str
    chunk_id: str
    quote: str
    start: int
    end: int
    page: dict[str, Any] | None
    page_end: dict[str, Any] | None
    source_title: str
    source_kind: str

    def to_json(self) -> dict[str, Any]:
        return {
            "sourceId": self.source_id,
            "versionId": self.version_id,
            "chunkId": self.chunk_id,
            "quote": self.quote,
            "offsets": {"start": self.start, "end": self.end},
            "page": self.page,
            "pageEnd": self.page_end,
            "sourceTitle": self.source_title,
            "sourceKind": self.source_kind,
        }


@dataclass
class VerifiedClaim:
    text: str
    citations: list[Resolved]


@dataclass
class VerificationStats:
    proposed_claims: int = 0
    dropped_claims: int = 0
    proposed_citations: int = 0
    dropped_citations: int = 0

    def to_json(self) -> dict[str, int]:
        return {
            "proposedClaims": self.proposed_claims,
            "droppedClaims": self.dropped_claims,
            "proposedCitations": self.proposed_citations,
            "droppedCitations": self.dropped_citations,
        }


def _page_json(pages: Sequence[Mapping[str, Any]] | None, offset: int) -> dict[str, Any] | None:
    ref = page_at(pages, offset)  # type: ignore[arg-type]
    if ref is None:
        return None
    return {"physical": ref.physical, "label": ref.label}


class CitationResolver:
    """Built once per run from the context (``chunks`` + ``versions`` as passed to the model)."""

    def __init__(
        self, chunks: Sequence[Mapping[str, Any]], versions: Mapping[str, Mapping[str, Any]]
    ) -> None:
        self._versions = versions
        self._chunks = {c["key"]: c for c in chunks}
        self._runs: list[_Run] = []
        by_version: dict[str, list[Mapping[str, Any]]] = {}
        for chunk in chunks:
            by_version.setdefault(chunk["version_id"], []).append(chunk)
        for version_id, items in by_version.items():
            items.sort(key=lambda c: c["ordinal"])
            run: _Run | None = None
            prev: Mapping[str, Any] | None = None
            for chunk in items:
                if run is not None and prev is not None and chunk["ordinal"] == prev["ordinal"] + 1:
                    # Chunks are separated by whitespace only; spaces keep offsets exact.
                    run.text += " " * (chunk["start"] - prev["end"]) + chunk["text"]
                    run.chunk_keys.add(chunk["key"])
                else:
                    run = _Run(version_id, chunk["start"], chunk["text"], {chunk["key"]})
                    self._runs.append(run)
                prev = chunk

    def _chunk_at(self, version_id: str, offset: int) -> str:
        for chunk in self._chunks.values():
            if chunk["version_id"] == version_id and chunk["start"] <= offset < chunk["end"]:
                return str(chunk["chunk_id"])
        return ""

    def resolve(self, chunk_key: str, quote: str) -> Resolved | None:
        named = self._chunks.get(chunk_key)
        if named is None:
            return None
        ordered = sorted(self._runs, key=lambda r: chunk_key not in r.chunk_keys)
        for run in ordered:
            match = find_quote(run.text, quote)
            if match is None:
                continue
            start, end = run.start + match.start, run.start + match.end
            version = self._versions[run.version_id]
            pages = version.get("pages")
            return Resolved(
                source_id=str(version["source_id"]),
                version_id=run.version_id,
                chunk_id=self._chunk_at(run.version_id, start),
                quote=run.text[match.start : match.end],
                start=start,
                end=end,
                page=_page_json(pages, start),
                page_end=_page_json(pages, max(end - 1, start)),
                source_title=str(version.get("title", "")),
                source_kind=str(version.get("kind", "")),
            )
        return None

    def verify_claims(
        self, raw_claims: Iterable[Mapping[str, Any]], stats: VerificationStats
    ) -> list[VerifiedClaim]:
        verified: list[VerifiedClaim] = []
        for raw in raw_claims:
            stats.proposed_claims += 1
            text = str(raw.get("text", "")).strip()
            kept: list[Resolved] = []
            seen: set[tuple[str, int, int]] = set()
            for citation in raw.get("citations", []) or []:
                stats.proposed_citations += 1
                resolved = self.resolve(
                    str(citation.get("chunk", "")), str(citation.get("quote", ""))
                )
                if resolved is None:
                    stats.dropped_citations += 1
                    continue
                ident = (resolved.version_id, resolved.start, resolved.end)
                if ident in seen or len(kept) >= MAX_CITATIONS_PER_CLAIM:
                    continue
                seen.add(ident)
                kept.append(resolved)
            if not text or not kept:
                stats.dropped_claims += 1
                continue
            verified.append(VerifiedClaim(text=text, citations=kept))
        return verified


@dataclass
class CitationTable:
    """Numbered citations of one answer/summary; claims refer to them by index."""

    items: list[Resolved] = field(default_factory=list)
    _index: dict[tuple[str, int, int], int] = field(default_factory=dict)

    def add(self, citation: Resolved) -> int:
        ident = (citation.version_id, citation.start, citation.end)
        if ident not in self._index:
            self._index[ident] = len(self.items)
            self.items.append(citation)
        return self._index[ident]

    def to_json(self) -> list[dict[str, Any]]:
        return [{"index": i + 1, **c.to_json()} for i, c in enumerate(self.items)]


def markers(indices: Sequence[int]) -> str:
    return "".join(f"[{i + 1}]" for i in indices)
