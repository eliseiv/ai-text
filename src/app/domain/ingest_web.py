"""One public web article: safe fetch + main-text extraction + snapshot.

SSRF is the threat model of this module — the server fetches a URL a user typed:

* only ``http``/``https``, default ports, no credentials in the URL;
* the host is resolved HERE and EVERY address must be globally routable (no loopback, private,
  link-local, CGNAT, metadata ``169.254.169.254``, multicast, reserved);
* the connection goes to the IP we just validated (``Host`` header + TLS SNI keep the virtual host
  and certificate check intact) — a DNS answer cannot change between check and connect
  (rebinding);
* redirects are followed MANUALLY and each hop is validated again;
* one page only, body size capped, streaming read, short timeout. No crawling.

The snapshot (raw bytes + final URL + fetch date) is stored: the answer cites what the page said
at import time, not what it says today.
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.domain.ingest_pdf import ExtractedDoc, ExtractionError
from app.domain.text import clean_text, has_meaningful_text

Resolver = Callable[[str, int], Awaitable[list[str]]]

_ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp")
_UNAVAILABLE = (
    "Не удалось загрузить статью. Возможно, сайт требует входа или блокирует загрузку. "
    "Скопируйте текст статьи и вставьте его как текст."
)


def validate_url(raw: str) -> str:
    """Syntactic check (request time). The network-level check happens again at fetch time."""
    url = raw.strip()
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ExtractionError("invalid_url", "Некорректная ссылка.") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ExtractionError("invalid_url", "Поддерживаются только ссылки http(s)://.")
    if parts.username or parts.password:
        raise ExtractionError("invalid_url", "Ссылки с логином и паролем не поддерживаются.")
    try:
        port = parts.port
    except ValueError as exc:
        raise ExtractionError("invalid_url", "Некорректная ссылка.") from exc
    if port not in (None, 80, 443):
        raise ExtractionError("invalid_url", "Поддерживаются только стандартные порты 80/443.")
    host = parts.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES) or "." not in host:
        raise ExtractionError("url_not_public", "Ссылка должна вести на публичный сайт.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not _is_public(ip):
        raise ExtractionError("url_not_public", "Ссылка должна вести на публичный сайт.")
    # Drop the fragment: it never reaches the server anyway.
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


@dataclass(frozen=True)
class FetchedPage:
    final_url: str
    content_type: str
    body: bytes
    fetched_at: datetime.datetime


class WebFetcher:
    def __init__(
        self,
        *,
        max_bytes: int,
        timeout: float,
        max_redirects: int,
        user_agent: str,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._timeout = timeout
        self._max_redirects = max_redirects
        self._user_agent = user_agent
        self._resolver = resolver
        self._transport = transport

    async def _pinned_target(self, url: str) -> tuple[str, str, str]:
        """``(url_with_ip, host_header, sni_host)`` after validating every resolved address."""
        url = validate_url(url)
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            addresses = await self._resolver(host, port)
        except OSError as exc:
            raise ExtractionError("web_unavailable", _UNAVAILABLE, retryable=True) from exc
        if not addresses:
            raise ExtractionError("web_unavailable", _UNAVAILABLE, retryable=True)
        ips = [ipaddress.ip_address(a) for a in addresses]
        if not all(_is_public(ip) for ip in ips):
            raise ExtractionError("url_not_public", "Ссылка должна вести на публичный сайт.")
        ip = ips[0]
        ip_host = f"[{ip}]" if ip.version == 6 else str(ip)
        netloc = ip_host if parts.port is None else f"{ip_host}:{parts.port}"
        pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        return pinned, parts.netloc, host

    async def fetch(self, url: str) -> FetchedPage:
        current = validate_url(url)
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            transport=self._transport,
            headers={"User-Agent": self._user_agent, "Accept": ",".join(_ALLOWED_TYPES)},
        ) as client:
            for _ in range(self._max_redirects + 1):
                pinned, host_header, sni = await self._pinned_target(current)
                request = client.build_request(
                    "GET", pinned, headers={"Host": host_header}, extensions={"sni_hostname": sni}
                )
                try:
                    response = await client.send(request, stream=True)
                except httpx.HTTPError as exc:
                    raise ExtractionError("web_unavailable", _UNAVAILABLE, retryable=True) from exc
                try:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ExtractionError("web_unavailable", _UNAVAILABLE)
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        retryable = response.status_code >= 500 or response.status_code == 429
                        raise ExtractionError("web_unavailable", _UNAVAILABLE, retryable=retryable)
                    content_type = response.headers.get("content-type", "").split(";")[0].strip()
                    if content_type.lower() not in _ALLOWED_TYPES:
                        raise ExtractionError(
                            "web_unsupported_content",
                            "По ссылке не статья (например, файл или изображение). "
                            "Для PDF используйте загрузку файла.",
                        )
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self._max_bytes:
                        raise ExtractionError("web_too_large", "Страница слишком большая.")
                    body = bytearray()
                    async for piece in response.aiter_bytes():
                        body.extend(piece)
                        if len(body) > self._max_bytes:
                            raise ExtractionError("web_too_large", "Страница слишком большая.")
                    return FetchedPage(
                        final_url=current,
                        content_type=content_type.lower(),
                        body=bytes(body),
                        fetched_at=datetime.datetime.now(tz=datetime.UTC),
                    )
                except httpx.HTTPError as exc:
                    raise ExtractionError("web_unavailable", _UNAVAILABLE, retryable=True) from exc
                finally:
                    await response.aclose()
        raise ExtractionError("web_unavailable", "Слишком много перенаправлений.")


def extract_article(page: FetchedPage) -> ExtractedDoc:
    """Main text of the page (boilerplate, menus and comments removed)."""
    title: str | None = None
    if page.content_type == "text/plain":
        text = clean_text(page.body.decode("utf-8", errors="replace"))
    else:
        import trafilatura
        from trafilatura.utils import load_html

        tree = load_html(page.body)
        if tree is None:
            raise ExtractionError("empty_text", _UNAVAILABLE)
        try:
            metadata = trafilatura.extract_metadata(tree, default_url=page.final_url)
            title = metadata.title if metadata is not None else None
        except Exception:  # noqa: BLE001 - metadata is optional
            title = None
        extracted = trafilatura.extract(
            load_html(page.body),
            url=page.final_url,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        text = clean_text(extracted or "")
    if not has_meaningful_text(text, 200):
        # Paywalls, JS-only pages, cookie walls: nothing honest to answer from.
        raise ExtractionError("empty_text", _UNAVAILABLE)
    return ExtractedDoc(
        text=text,
        title=(title or "").strip()[:200] or None,
        meta={
            "url": page.final_url,
            "fetchedAt": page.fetched_at.isoformat(),
            "contentType": page.content_type,
        },
    )
