"""Pure units of the citation guarantee, PDF/web ingestion and the demo content."""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest

from app.domain.citations import CitationResolver, VerificationStats
from app.domain.demo import DEMO_TEXT, build_demo_summary
from app.domain.ingest_pdf import ExtractionError, extract_pdf, inspect_pdf
from app.domain.ingest_web import FetchedPage, WebFetcher, extract_article, validate_url
from app.domain.text import chunk_text, clean_text, find_quote, normalize
from tests.domain.conftest import make_pdf


# --- quote matching ----------------------------------------------------------------------------
def test_find_quote_forgives_typography_not_wording() -> None:
    source = "Он сказал: «Ёлка — это    дерево»,\nа потом ушёл домой."
    match = find_quote(source, 'он сказал: "елка - это дерево", а потом')
    assert match is not None
    assert source[match.start : match.end].startswith("Он сказал")
    assert find_quote(source, "Он заявил, что ёлка — дерево") is None  # paraphrase
    assert find_quote(source, "Ёлка") is None  # too short to prove anything


def test_find_quote_joins_pdf_hyphenation_and_honours_ellipsis() -> None:
    source = "Эта инфор-\nмация важна для всех. Середина текста. Конец важной мысли здесь."
    m = find_quote(source, "Эта информация важна для всех")
    assert m is not None and source[m.start : m.end].startswith("Эта инфор-")
    m2 = find_quote(source, "Эта информация важна … важной мысли здесь")
    assert m2 is not None and source[m2.end - 5 : m2.end] == "здесь"


def test_chunks_are_exact_slices_and_do_not_overlap() -> None:
    text = clean_text(("Первое предложение абзаца. " * 40 + "\n\n") * 6)
    spans = chunk_text(text, target=500)
    assert len(spans) > 5
    prev_end = 0
    for span in spans:
        assert text[span.start : span.end] == span.text
        assert span.start >= prev_end
        assert text[prev_end : span.start].strip() == ""
        prev_end = span.end
    assert normalize(text[prev_end:]) == ""


# --- citation resolver -------------------------------------------------------------------------
def _context(text: str, pages: Any = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spans = chunk_text(text, target=120)
    chunks = [
        {
            "key": f"c{s.ordinal + 1}",
            "chunk_id": f"id{s.ordinal}",
            "version_id": "v1",
            "ordinal": s.ordinal,
            "start": s.start,
            "end": s.end,
            "text": s.text,
        }
        for s in spans
    ]
    return chunks, {"v1": {"source_id": "s1", "title": "Док", "kind": "pdf", "pages": pages}}


def test_resolver_returns_original_slice_across_chunk_boundary_with_pages() -> None:
    text = (
        "Alpha beta gamma delta epsilon zeta eta theta. " * 3
        + "\n\n"
        + "Iota kappa lambda mu nu xi omicron pi rho sigma. " * 3
    )
    boundary = text.index("Iota")
    pages = [
        {"n": 1, "label": "i", "start": 0, "end": boundary - 2},
        {"n": 2, "label": "1", "start": boundary, "end": len(text)},
    ]
    chunks, versions = _context(text, pages)
    resolver = CitationResolver(chunks, versions)
    quote = "theta.\n\nIota kappa lambda"
    resolved = resolver.resolve("c1", quote)
    assert resolved is not None
    assert text[resolved.start : resolved.end] == resolved.quote
    assert resolved.page == {"physical": 1, "label": "i"}
    assert resolved.page_end == {"physical": 2, "label": "1"}


def test_resolver_drops_unknown_chunks_invented_quotes_and_uncited_claims() -> None:
    chunks, versions = _context("Solar panels convert sunlight into electricity efficiently. " * 4)
    stats = VerificationStats()
    claims = CitationResolver(chunks, versions).verify_claims(
        [
            {
                "text": "ok",
                "citations": [{"chunk": "c1", "quote": "convert sunlight into electricity"}],
            },
            {"text": "foreign", "citations": [{"chunk": "c999", "quote": "convert sunlight into"}]},
            {"text": "invented", "citations": [{"chunk": "c1", "quote": "wind turbines are best"}]},
            {"text": "uncited", "citations": []},
        ],
        stats,
    )
    assert [c.text for c in claims] == ["ok"]
    assert stats.dropped_claims == 3 and stats.dropped_citations == 2


def test_demo_summary_quotes_are_real() -> None:
    text = DEMO_TEXT.strip()
    spans = chunk_text(text)
    chunks = [
        {
            "key": f"c{s.ordinal + 1}",
            "chunk_id": "x",
            "version_id": "v",
            "ordinal": s.ordinal,
            "start": s.start,
            "end": s.end,
            "text": s.text,
        }
        for s in spans
    ]
    content = build_demo_summary(
        chunks, {"v": {"source_id": "s", "title": "t", "kind": "text", "pages": None}}
    )
    for citation in content["citations"]:
        start, end = citation["offsets"]["start"], citation["offsets"]["end"]
        assert text[start:end] == citation["quote"]


# --- PDF ---------------------------------------------------------------------------------------
_PAGE = "The mitochondria is the powerhouse of the cell and produces ATP for energy. "


def test_pdf_text_pages_and_printed_labels() -> None:
    data = make_pdf(
        [_PAGE * 2, "Second page talks about ribosomes and protein synthesis. " * 2],
        labels_start_roman=True,
    )
    assert inspect_pdf(data, max_pages=10).page_count == 2
    doc = extract_pdf(data, max_pages=10, min_chars_per_page=20, min_text_page_ratio=0.3)
    assert "powerhouse" in doc.text and "ribosomes" in doc.text
    assert [(p["n"], p["label"]) for p in doc.pages or []] == [(1, "i"), (2, "1")]
    for page in doc.pages or []:
        assert doc.text[page["start"] : page["end"]].strip()


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"hello world, not a pdf", "unsupported_file"),
        (b"%PDF-1.4\n garbage without objects", "pdf_corrupted"),
    ],
)
def test_pdf_rejects_non_pdf_and_corrupted(data: bytes, code: str) -> None:
    with pytest.raises(ExtractionError) as exc:
        inspect_pdf(data, max_pages=10)
    assert exc.value.code == code


def test_pdf_rejects_password_scans_and_too_many_pages() -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(make_pdf([_PAGE]))).pages:
        writer.add_page(page)
    writer.encrypt(user_password="secret", owner_password="owner")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ExtractionError) as exc:
        inspect_pdf(buffer.getvalue(), max_pages=10)
    assert exc.value.code == "pdf_password_protected"

    scan = make_pdf(["", "", "", _PAGE])  # 1 of 4 pages has text → below 30 %
    with pytest.raises(ExtractionError) as exc:
        extract_pdf(scan, max_pages=10, min_chars_per_page=20, min_text_page_ratio=0.3)
    assert exc.value.code == "pdf_no_text_layer"

    with pytest.raises(ExtractionError) as exc:
        inspect_pdf(make_pdf([_PAGE] * 3), max_pages=2)
    assert exc.value.code == "pdf_too_many_pages"


def test_pdf_partial_text_layer_is_reported_not_hidden() -> None:
    doc = extract_pdf(
        make_pdf([_PAGE, "", _PAGE]), max_pages=10, min_chars_per_page=20, min_text_page_ratio=0.3
    )
    assert doc.meta["pagesWithoutText"] == [2]


# --- web / SSRF --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/a",
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://user:pass@example.com/",
        "http://example.com:8080/",
        "http://intranet/",
        "http://service.internal/",
    ],
)
def test_validate_url_rejects_non_public_targets(url: str) -> None:
    with pytest.raises(ExtractionError):
        validate_url(url)


def _fetcher(resolve: dict[str, list[str]], handler: Any) -> WebFetcher:
    async def resolver(host: str, port: int) -> list[str]:
        return resolve[host]

    return WebFetcher(
        max_bytes=100_000,
        timeout=5,
        max_redirects=3,
        user_agent="t",
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )


async def test_fetch_blocks_dns_pointing_inside_and_redirects_to_private() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x")

    with pytest.raises(ExtractionError) as exc:
        await _fetcher({"evil.example": ["10.1.2.3"]}, ok).fetch("https://evil.example/a")
    assert exc.value.code == "url_not_public"

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://metadata.example/"})

    fetcher = _fetcher(
        {"news.example": ["93.184.216.34"], "metadata.example": ["169.254.169.254"]}, redirect
    )
    with pytest.raises(ExtractionError) as exc:
        await fetcher.fetch("https://news.example/story")
    assert exc.value.code == "url_not_public"


async def test_fetch_connects_to_the_validated_ip_and_extracts_the_article() -> None:
    seen: list[httpx.Request] = []
    article = (
        "<p>" + "Статья о том, как устроена память человека и почему мы забываем. " * 10 + "</p>"
    )
    html = (
        "<html><head><title>Память</title></head><body><nav>menu</nav>"
        f"<article>{article}</article></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=html)

    page = await _fetcher({"news.example": ["93.184.216.34"]}, handler).fetch(
        "https://news.example/story#x"
    )
    assert seen[0].url.host == "93.184.216.34"  # pinned: no second DNS lookup by the client
    assert seen[0].headers["host"] == "news.example"
    assert seen[0].extensions["sni_hostname"] == "news.example"
    doc = extract_article(page)
    assert "почему мы забываем" in doc.text and doc.title == "Память"
    assert doc.meta["url"] == "https://news.example/story"


def test_article_without_text_suggests_pasting() -> None:
    page = FetchedPage(
        "https://x.example/",
        "text/html",
        b"<html><body><div>login</div></body></html>",
        __import__("datetime").datetime.now(),
    )
    with pytest.raises(ExtractionError) as exc:
        extract_article(page)
    assert exc.value.code == "empty_text"
