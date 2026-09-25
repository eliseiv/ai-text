"""Text primitives: cleaning, chunking, and quote matching with EXACT offsets.

The citation guarantee of the product rests on this module: a quote is accepted only if it is a
real substring of the source text (after a normalization that forgives typography, not wording),
and the offsets it returns always index the ORIGINAL version text — so the client highlights the
real fragment, never an approximation.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

_QUOTES = {
    "«": '"',
    "»": '"',
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "″": '"',
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "′": "'",
    "`": "'",
}
_DASHES = set("‐‑‒–—―−﹘﹣－")
_SOFT_HYPHEN = "­"
_ELLIPSIS_SPLIT = re.compile(r"\s*(?:\.\.\.|…)\s*")

# Quotes shorter than this (normalized) are too generic to prove anything.
MIN_QUOTE_CHARS = 12
# An elided quote ("начало … конец") may span at most this many original characters.
_MAX_ELIDED_SPAN = 2000


def clean_text(raw: str) -> str:
    """Canonical storage form of extracted text (applied ONCE, before versioning).

    Only invisible/garbage characters are touched: NUL, BOM, non-standard line breaks. Wording and
    typography are preserved — the stored text is what the user sees as «оригинал».
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "").replace("﻿", "")
    text = re.sub(r"[  ]", "\n", text)
    text = re.sub(r"[ \t\f\v]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def has_meaningful_text(text: str, min_chars: int = 1) -> bool:
    return len(re.sub(r"\s+", "", text)) >= min_chars


def _map_char(c: str) -> str:
    if c in _QUOTES:
        return _QUOTES[c]
    if c in _DASHES:
        return "-"
    if c.isspace():
        return " "
    mapped = unicodedata.normalize("NFKC", c).lower()
    return mapped.replace("ё", "е")


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalize for matching and keep, for every output char, its index in ``text``.

    Forgiven: case, ё/е, quote and dash styles, whitespace runs, soft hyphens, and PDF line-break
    hyphenation («инфор-\\nмация» == «информация»).
    """
    out: list[str] = []
    index: list[int] = []
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c == _SOFT_HYPHEN:
            i += 1
            continue
        if c == "-" and out and out[-1].isalpha():
            j = i + 1
            saw_newline = False
            while j < n and text[j].isspace():
                saw_newline = saw_newline or text[j] == "\n"
                j += 1
            if saw_newline and j < n and text[j].isalpha() and text[j].islower():
                i = j  # drop the hyphen and the line break: the word continues
                continue
        mapped = _map_char(c)
        if mapped == " ":
            if out and out[-1] != " ":
                out.append(" ")
                index.append(i)
        else:
            for ch in mapped:
                out.append(ch)
                index.append(i)
        i += 1
    return "".join(out), index


def normalize(text: str) -> str:
    return normalize_with_map(text)[0].strip()


@dataclass(frozen=True)
class Match:
    start: int  # offsets in the ORIGINAL text, end-exclusive
    end: int


def find_quote(haystack: str, quote: str) -> Match | None:
    """Locate ``quote`` in ``haystack``; ``None`` unless it is really there.

    An ellipsis in the quote is honoured as an elision: every part must occur, in order, within
    a bounded span. Anything else (paraphrase, a translated quote) does not match — by design.
    """
    norm_hay, index = normalize_with_map(haystack)
    parts = [normalize(p) for p in _ELLIPSIS_SPLIT.split(quote)]
    parts = [p for p in parts if p]
    if not parts or sum(len(p) for p in parts) < MIN_QUOTE_CHARS:
        return None
    if len(parts) == 1:
        pos = norm_hay.find(parts[0])
        if pos < 0:
            return None
        return Match(index[pos], index[pos + len(parts[0]) - 1] + 1)

    search_from = 0
    while True:
        first = norm_hay.find(parts[0], search_from)
        if first < 0:
            return None
        cursor = first + len(parts[0])
        ok = True
        for part in parts[1:]:
            pos = norm_hay.find(part, cursor)
            if pos < 0:
                ok = False
                break
            cursor = pos + len(part)
        if ok:
            start, end = index[first], index[cursor - 1] + 1
            if end - start <= _MAX_ELIDED_SPAN:
                return Match(start, end)
        if not ok:
            return None
        search_from = first + 1


# --- chunking ----------------------------------------------------------------------------------
_SENTENCE_END = re.compile(r"[.!?…][\"'»”)\]]*\s")


@dataclass(frozen=True)
class ChunkSpan:
    ordinal: int
    start: int
    end: int
    text: str


def chunk_text(text: str, target: int = 1500) -> list[ChunkSpan]:
    """Split into chunks of ~``target`` chars at paragraph → sentence → word boundaries.

    Invariant (tested): ``chunk.text == text[chunk.start:chunk.end]`` and chunks never overlap —
    offsets inside a chunk translate to version offsets by plain addition.
    """
    spans: list[ChunkSpan] = []
    n = len(text)
    pos = 0
    min_len = max(target // 2, 1)
    while pos < n:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            break
        hard_end = min(pos + target, n)
        end = hard_end
        if hard_end < n:
            window = text[pos + min_len : hard_end]
            cut = window.rfind("\n\n")
            if cut >= 0:
                end = pos + min_len + cut + 2
            else:
                last = None
                for m in _SENTENCE_END.finditer(window):
                    last = m
                if last is not None:
                    end = pos + min_len + last.end()
                else:
                    space = window.rfind(" ")
                    if space >= 0:
                        end = pos + min_len + space + 1
        stop = end
        while stop > pos and text[stop - 1].isspace():
            stop -= 1
        if stop > pos:
            spans.append(ChunkSpan(len(spans), pos, stop, text[pos:stop]))
        pos = end
    return spans


# --- pages -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PageRef:
    physical: int  # 1-based index of the page in the file
    label: str | None  # printed page number (PDF /PageLabels), None when the file has none


def page_at(pages: Sequence[dict[str, object]] | None, offset: int) -> PageRef | None:
    """The page containing ``offset``. ``None`` for text/web sources: no invented pages."""
    if not pages:
        return None
    starts = [int(p["start"]) for p in pages]  # type: ignore[call-overload]
    i = bisect.bisect_right(starts, offset) - 1
    if i < 0:
        i = 0
    page = pages[i]
    label = page.get("label")
    return PageRef(physical=int(page["n"]), label=str(label) if label else None)  # type: ignore[call-overload]
