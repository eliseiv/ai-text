"""Redis sliding-window rate limiting.

Sorted-set sliding window; limits come from config. Redis is the source of truth, and on a Redis
outage every limiter **fails OPEN** (availability over strictness on this path) with a WARNING —
rate limiting is an abuse guard, not a money invariant.

Classes:
``/v1/auth/*`` (per IP) · ``/v1/admin/*`` (per IP) · the PUBLIC CloudPayments webhook (per IP) ·
``POST /v1/generate`` (per user + per IP) · everything else (per user).
"""

from __future__ import annotations

import logging
import time
import uuid

import redis.asyncio as redis

from app.config import get_settings
from app.observability.logging import log_event

logger = logging.getLogger("app.api_gateway.rate_limit")

_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(  # type: ignore[no-untyped-call]
            get_settings().redis_url, decode_responses=True
        )
    return _redis_client


async def _allow(client: redis.Redis, key: str, limit: int, window_seconds: int) -> bool:
    now = time.time()
    member = f"{now}:{uuid.uuid4()}"
    cutoff = now - window_seconds
    async with client.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zadd(key, {member: now})
        pipe.zcard(key)
        pipe.expire(key, window_seconds)
        results = await pipe.execute()
    count = int(results[2])
    return count <= limit


async def enforce_generation_limits(*, user_id: uuid.UUID, ip: str | None) -> bool:
    """Limit for ``POST /v1/generate`` (the domain generation route). True = allowed.

    Tighter than the general per-user limit: a generation costs an upstream call and a DB
    connection for however long the provider takes.
    """
    settings = get_settings()
    client = get_redis()
    window = settings.rate_limit_window_seconds
    checks: list[tuple[str, int]] = [
        (f"rl:gen:user:{user_id}", settings.rate_limit_generation_per_user),
    ]
    if ip:
        checks.append((f"rl:ip:{ip}", settings.rate_limit_per_ip))
    try:
        for key, limit in checks:
            if not await _allow(client, key, limit, window):
                return False
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "rate_limit_redis_unavailable", error=str(exc))
        return True  # fail open
    return True


async def enforce_other_limits(*, user_id: uuid.UUID) -> bool:
    """General per-user limit for the remaining authenticated endpoints."""
    settings = get_settings()
    client = get_redis()
    try:
        return await _allow(
            client,
            f"rl:other:{user_id}",
            settings.rate_limit_per_user,
            settings.rate_limit_window_seconds,
        )
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "rate_limit_redis_unavailable", error=str(exc))
        return True


async def enforce_auth_limits(*, ip: str | None) -> bool:
    """Per-IP limit on ``/v1/auth/*`` — the endpoints are public (no JWT), so the
    source IP is the only throttle. An unresolvable IP shares one bucket, so the surface is never
    left fully unlimited."""
    settings = get_settings()
    client = get_redis()
    bucket = ip or "unknown"
    try:
        return await _allow(
            client,
            f"rl:auth:{bucket}",
            settings.auth_rate_limit_per_ip,
            settings.rate_limit_window_seconds,
        )
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "rate_limit_redis_unavailable", error=str(exc))
        return True


async def enforce_admin_limits(*, ip: str | None) -> bool:
    """Dedicated per-IP limit on ``/v1/admin/*``, isolated from user limits."""
    settings = get_settings()
    client = get_redis()
    bucket = ip or "unknown"
    try:
        return await _allow(
            client,
            f"rl:admin:{bucket}",
            settings.admin_rate_limit_per_min,
            settings.rate_limit_window_seconds,
        )
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "rate_limit_redis_unavailable", error=str(exc))
        return True


async def enforce_cloudpayments_webhook_limits(*, ip: str | None) -> bool:
    """Per-IP limit on the PUBLIC CloudPayments webhook.

    The aggregator sends no auth, so the endpoint is open; the cap is generous on purpose — its
    job is anti-amplification of OUR outgoing verification GET, not blocking legitimate callbacks.
    """
    settings = get_settings()
    client = get_redis()
    bucket = ip or "unknown"
    try:
        return await _allow(
            client,
            f"rl:cpwebhook:{bucket}",
            settings.cloudpayments_webhook_rate_limit_per_ip,
            settings.rate_limit_window_seconds,
        )
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "rate_limit_redis_unavailable", error=str(exc))
        return True


async def redis_ping() -> bool:
    try:
        return bool(await get_redis().ping())
    except redis.RedisError:
        return False


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
    _redis_client = None
