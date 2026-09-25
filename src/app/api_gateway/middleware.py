"""Gateway middleware: correlation id, size limit, security headers.

Neither middleware contains a single domain path — that is the point. Both take
their domain-specific rules as DATA from ``DomainRegistry``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.extensions.registry import BodyLimitRule
from app.observability.context import set_generation_id, set_request_id, set_user_id
from app.observability.metrics import http_responses_total


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Generates/propagates ``X-Request-Id`` (HTTP correlation id, NOT a billing key).

    Also counts responses by status (``http_responses_total``) — this is the only place that sees
    every response, including the 429s produced by rate limiting.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        set_request_id(request_id)
        set_generation_id(None)
        set_user_id(None)
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        http_responses_total.labels(status=str(response.status_code)).inc()
        return response


class SizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects oversized bodies BEFORE parsing (``413``) — the memory-DoS guard.

    The general limit (``SIZE_LIMIT_BODY``, 512 KB) applies to every route. Routes that accept
    inline base64 need more (base64 inflates by 4/3), and they declare it themselves through
    ``DomainRegistry.body_limit_rules``.

    In the source the exceptions were HARDCODED here (``/v1/chat/run``), and the file-upload route
    was simply forgotten — its advertised 8 MB limit was cut at 512 KB in the gateway, before the
    application validator ran. Real ceiling: ~375 KB. The fix is not "remember the path" but "the
    core must not know domain paths at all".

    The raise stays POINT-WISE: bumping the general limit instead would open memory-DoS on every
    route, including those that accept tiny JSON.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int,
        rules: Sequence[BodyLimitRule] = (),
    ) -> None:
        super().__init__(app)
        self._default_limit = default_limit
        self._rules = tuple(rules)

    def _limit_for(self, request: Request) -> int:
        for rule in self._rules:
            if rule.matches(request.url.path, request.method):
                return rule.limit
        return self._default_limit

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._limit_for(request):
                    return self._too_large(request)
            except ValueError:
                pass  # unparsable Content-Length: let the ASGI server reject the request
        return await call_next(request)

    def _too_large(self, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "payload_too_large",
                    "message": "request body exceeds limit",
                    "requestId": request_id,
                }
            },
        )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Default API security headers.

    A route that serves its OWN complete header set (e.g. a domain preview endpoint returning
    user-generated HTML behind a CSP sandbox, which needs ``X-Frame-Options: SAMEORIGIN`` instead
    of the API default ``DENY``) registers its prefix in
    ``DomainRegistry.security_headers_exempt_prefixes``. The core hardcodes no such prefix.
    """

    def __init__(self, app: ASGIApp, *, exempt_prefixes: Sequence[str] = ()) -> None:
        super().__init__(app)
        self._exempt_prefixes = tuple(exempt_prefixes)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response: Response = await call_next(request)
        if any(request.url.path.startswith(prefix) for prefix in self._exempt_prefixes):
            return response
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response
