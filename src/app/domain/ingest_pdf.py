"""PDF: validation and text extraction (text layer only — no OCR in MVP).

Two stages with the SAME checks, different timing:

* ``inspect_pdf`` — in the upload request: type, size, corruption, password, page count. Cheap,
  so the user gets the reason immediately instead of a failed status a minute later;
* ``extract_pdf`` — in the worker: per-page text, printed page labels, scan detection.

Physical vs printed numbering is kept apart on purpose: ``n`` is the page index in the file,
``label`` is what is printed on the page (``/PageLabels``: «xii», «А-3»). A file without labels
gets ``label=None`` — we never pretend the printed number equals the index.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any

from app.domain.text import clean_text, has_meaningful_text

logger = logging.getLogger("app.domain.ingest_pdf")

PDF_MAGIC = b"%PDF-"


class ExtractionError(Exception):
    """A user-explainable rejection. ``message`` is shown to the user as is (Russian)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    title: str | None


@dataclass
class ExtractedDoc:
    text: str
    title: str | None = None
    pages: list[dict[str, Any]] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _open(data: bytes) -> Any:
    from pypdf import PasswordType, PdfReader

    if not data.startswith(PDF_MAGIC) and PDF_MAGIC not in data[:1024]:
        raise ExtractionError("unsupported_file", "Файл не является PDF-документом.")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            raise ExtractionError(
                "pdf_password_protected",
                "PDF защищён паролем. Снимите защиту и загрузите файл снова.",
            )
        _ = len(reader.pages)
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types for broken files
        raise ExtractionError(
            "pdf_corrupted", "PDF повреждён или не читается. Попробуйте сохранить его заново."
        ) from exc
    return reader


def _title(reader: Any) -> str | None:
    try:
        meta = reader.metadata
        title = (meta.title if meta else None) or None
    except Exception:  # noqa: BLE001 - broken metadata must not fail the import
        return None
    if title and has_meaningful_text(str(title), 3):
        return str(title).strip()[:200]
    return None


def inspect_pdf(data: bytes, *, max_pages: int) -> PdfInfo:
    reader = _open(data)
    count = len(reader.pages)
    if count == 0:
        raise ExtractionError("pdf_empty", "В PDF нет страниц.")
    if count > max_pages:
        raise ExtractionError(
            "pdf_too_many_pages",
            f"В PDF {count} стр. — больше допустимых {max_pages}. Разделите файл на части.",
        )
    return PdfInfo(page_count=count, title=_title(reader))


def extract_pdf(
    data: bytes, *, max_pages: int, min_chars_per_page: int, min_text_page_ratio: float
) -> ExtractedDoc:
    reader = _open(data)
    count = len(reader.pages)
    if count > max_pages:
        raise ExtractionError(
            "pdf_too_many_pages",
            f"В PDF {count} стр. — больше допустимых {max_pages}. Разделите файл на части.",
        )
    try:
        has_labels = "/PageLabels" in reader.trailer["/Root"]
        labels: list[str] = list(reader.page_labels) if has_labels else []
    except Exception:  # noqa: BLE001 - malformed label tree: fall back to physical numbers only
        has_labels, labels = False, []

    parts: list[str] = []
    pages: list[dict[str, Any]] = []
    without_text: list[int] = []
    offset = 0
    for i, page in enumerate(reader.pages):
        try:
            raw = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one broken page must not lose the document
            raw = ""
        page_text = clean_text(raw)
        if not has_meaningful_text(page_text, min_chars_per_page):
            without_text.append(i + 1)
        if parts:
            parts.append("\n\n")
            offset += 2
        start = offset
        parts.append(page_text)
        offset += len(page_text)
        label = labels[i] if has_labels and i < len(labels) else None
        pages.append({"n": i + 1, "label": label, "start": start, "end": offset})

    text_pages = count - len(without_text)
    if count and text_pages / count < min_text_page_ratio:
        raise ExtractionError(
            "pdf_no_text_layer",
            "Похоже, это скан без текстового слоя. Распознавание сканов (OCR) пока не "
            "поддерживается — загрузите PDF с текстом.",
        )
    text = "".join(parts)
    if not has_meaningful_text(text, min_chars_per_page):
        raise ExtractionError("empty_text", "В PDF не найден текст.")
    return ExtractedDoc(
        text=text,
        title=_title(reader),
        pages=pages,
        meta={
            "pageCount": count,
            "hasPageLabels": has_labels,
            # Honest partial reading: pages without a text layer are listed, not hidden.
            "pagesWithoutText": without_text,
        },
    )
