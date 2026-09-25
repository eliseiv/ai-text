"""Domain fixtures: tmp file storage, a worker on the test DB with the fake LLM, PDF builder."""

from __future__ import annotations

import datetime
import uuid
import zlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.domain.config import domain_settings
from app.domain.ingest_web import FetchedPage, WebFetcher
from app.domain.llm import FakeLLMClient
from app.domain.sources import get_storage
from app.domain.storage import LocalFileStorage
from app.domain.worker import Worker, build_provider
from tests.conftest import auth_headers, seed_user

SIGNING_SECRET = "test-files-secret"


@pytest.fixture(autouse=True)
def domain_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    files = tmp_path / "files"
    monkeypatch.setenv("FILES_DIR", str(files))
    monkeypatch.setenv("FILES_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("PURGE_DELAY_SECONDS", "0")
    monkeypatch.setenv("CITATION_SUPPORT_CHECK", "true")
    get_settings.cache_clear()
    get_storage.cache_clear()
    yield str(files)
    get_settings.cache_clear()
    get_storage.cache_clear()


class FakeFetcher(WebFetcher):
    """The network boundary of web import. ``pages[url]`` → body, or an exception to raise."""

    def __init__(self) -> None:
        super().__init__(max_bytes=10**6, timeout=1, max_redirects=1, user_agent="t")
        self.pages: dict[str, Any] = {}

    async def fetch(self, url: str) -> FetchedPage:
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return FetchedPage(
            final_url=url,
            content_type="text/html",
            body=value.encode(),
            fetched_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
        )


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def fake_fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def worker(
    sessionmaker_: async_sessionmaker[AsyncSession],
    fake_llm: FakeLLMClient,
    fake_fetcher: FakeFetcher,
) -> Worker:
    settings = domain_settings()
    return Worker(
        sessionmaker=sessionmaker_,
        settings=settings,
        storage=LocalFileStorage(settings.files_dir),
        fetcher=fake_fetcher,
        provider=build_provider(settings, fake_llm, sessionmaker_),
    )


@pytest.fixture
async def paid_user(session: AsyncSession) -> AsyncIterator[uuid.UUID]:
    yield await seed_user(session, subscription="active", balance=100)


def headers(user_id: uuid.UUID, key: str | None = None) -> dict[str, str]:
    h = auth_headers(user_id)
    if key:
        h["Idempotency-Key"] = key
    return h


async def create_topic(client: httpx.AsyncClient, user_id: uuid.UUID, title: str = "Тема") -> str:
    r = await client.post("/v1/topics", json={"title": title}, headers=headers(user_id))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def add_text(
    client: httpx.AsyncClient, user_id: uuid.UUID, topic_id: str, body: str, title: str = "Текст"
) -> dict[str, Any]:
    r = await client.post(
        f"/v1/topics/{topic_id}/sources/text",
        json={"title": title, "text": body},
        headers=headers(user_id),
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def balance(session: AsyncSession, user_id: uuid.UUID) -> int:
    value = await session.scalar(
        text("SELECT balance FROM wallets WHERE user_id = :u"), {"u": str(user_id)}
    )
    return int(value or 0)


# --- a real, minimal PDF with a text layer (no external tools) ---------------------------------
def make_pdf(pages: list[str], *, labels_start_roman: bool = False) -> bytes:
    """Build a valid PDF: one Helvetica text line per page (latin-1 text only)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    pages_id = len(objects) + 2 * len(pages) + 1
    for content in pages:
        lines = [content[i : i + 80] for i in range(0, len(content), 80)] or [""]
        ops = ["BT /F1 11 Tf 40 780 Td 14 TL"]
        for line in lines:
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({safe}) Tj T*")
        ops.append("ET")
        stream = zlib.compress("\n".join(ops).encode("latin-1"))
        cid = add(
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream)
            + stream
            + b"\nendstream"
        )
        page_ids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                % (pages_id, font, cid)
            )
        )
    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))) == pages_id
    labels = b""
    if labels_start_roman:
        # Page 1 is printed as "i", pages 2+ as 1, 2, … — physical ≠ printed numbering.
        labels = b" /PageLabels << /Nums [0 << /S /r >> 1 << /S /D >>] >>"
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R%s >>" % (pages_id, labels))

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog,
        xref,
    )
    return bytes(out)
