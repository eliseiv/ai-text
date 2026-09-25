"""Access to ``generations`` — the anchor, the finalization, the reads.

Nothing here decides anything about money; it only writes what the service tells it. The money
invariants live in the DB (``ck_generations_charge_only_succeeded`` and friends), which is why they
survive a bug in this file.
"""

from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation.contract import GenerationUsage


@dataclass(frozen=True)
class GenerationRow:
    id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    provider: str
    model: str | None
    status: str
    credits_charged: int
    billing_kind: str
    units: int
    unit_kind: str
    total_tokens: int
    latency_ms: int | None
    error_code: str | None
    provider_ref: str | None
    meta: dict[str, Any]
    attempt: int
    created_at: datetime.datetime
    completed_at: datetime.datetime | None


_SELECT_COLUMNS = (
    "id, user_id, kind, provider, model, status, credits_charged, billing_kind, units, "
    "unit_kind, total_tokens, latency_ms, error_code, provider_ref, meta, attempt, created_at, "
    "completed_at"
)


def _row(r: Any) -> GenerationRow:
    return GenerationRow(
        id=r[0],
        user_id=r[1],
        kind=r[2],
        provider=r[3],
        model=r[4],
        status=r[5],
        credits_charged=int(r[6]),
        billing_kind=r[7],
        units=int(r[8]),
        unit_kind=r[9],
        total_tokens=int(r[10]),
        latency_ms=r[11],
        error_code=r[12],
        provider_ref=r[13],
        meta=r[14] or {},
        attempt=int(r[15]),
        created_at=r[16],
        completed_at=r[17],
    )


class GenerationsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_inflight(self, user_id: uuid.UUID) -> int:
        """Backed by the partial index ``ix_generations_inflight`` — the guard is cheap."""
        value = await self._session.scalar(
            text(
                "SELECT count(*) FROM generations "
                "WHERE user_id = :uid AND status IN ('pending','running')"
            ),
            {"uid": str(user_id)},
        )
        return int(value or 0)

    async def anchor(
        self,
        *,
        user_id: uuid.UUID,
        kind: str,
        provider: str,
        model: str | None,
        idempotency_key: str,
    ) -> uuid.UUID | None:
        """THE idempotency anchor. ``None`` ⇒ this request already exists (see the service).

        ``status='running'`` from the start: the row means "the provider is being called", which is
        exactly the invariant "every row = the provider was called".
        """
        inserted = await self._session.scalar(
            text(
                "INSERT INTO generations (user_id, kind, provider, model, status, idempotency_key) "
                "VALUES (:uid, :kind, :provider, :model, 'running', :key) "
                "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
                "RETURNING id"
            ),
            {
                "uid": str(user_id),
                "kind": kind,
                "provider": provider,
                "model": model,
                "key": idempotency_key,
            },
        )
        return uuid.UUID(str(inserted)) if inserted is not None else None

    async def get_by_key(self, user_id: uuid.UUID, idempotency_key: str) -> GenerationRow | None:
        row = (
            await self._session.execute(
                text(
                    f"SELECT {_SELECT_COLUMNS} FROM generations "
                    "WHERE user_id = :uid AND idempotency_key = :key"
                ),
                {"uid": str(user_id), "key": idempotency_key},
            )
        ).first()
        return _row(row) if row else None

    async def get(self, user_id: uuid.UUID, generation_id: uuid.UUID) -> GenerationRow | None:
        """Owner-scoped: a foreign generation is indistinguishable from a missing one (404)."""
        row = (
            await self._session.execute(
                text(
                    f"SELECT {_SELECT_COLUMNS} FROM generations WHERE id = :gid AND user_id = :uid"
                ),
                {"gid": str(generation_id), "uid": str(user_id)},
            )
        ).first()
        return _row(row) if row else None

    async def retry(self, generation_id: uuid.UUID) -> None:
        """A previously FAILED generation may be retried on the same key: attempt += 1.

        ``error_code`` must be cleared — ``ck_generations_error_code`` allows it only on ``failed``.
        """
        await self._session.execute(
            text(
                "UPDATE generations SET status = 'running', attempt = attempt + 1, "
                "error_code = NULL, error_message = NULL, completed_at = NULL, updated_at = now() "
                "WHERE id = :gid"
            ),
            {"gid": str(generation_id)},
        )

    async def fail(
        self, generation_id: uuid.UUID, *, error_code: str, error_message: str | None
    ) -> None:
        """A provider failure. NO debit — and the DB CHECK would refuse one anyway (BR-7)."""
        await self._session.execute(
            text(
                "UPDATE generations SET status = 'failed', error_code = :code, "
                "error_message = :msg, completed_at = now(), updated_at = now() "
                "WHERE id = :gid"
            ),
            {
                "gid": str(generation_id),
                "code": error_code,
                # redacted + bounded: no keys, no user content, <= 1 KB
                "msg": (error_message or "")[:1024] or None,
            },
        )

    async def finalize(
        self,
        generation_id: uuid.UUID,
        *,
        usage: GenerationUsage,
        credits_charged: int,
        billing_kind: str,
        ledger_tx_id: uuid.UUID | None,
        latency_ms: int,
        stop_reason: str | None,
        provider_ref: str | None,
        meta: dict[str, Any],
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE generations SET status = 'succeeded', "
                "input_tokens = :in_tok, output_tokens = :out_tok, "
                "cache_read_tokens = :cr_tok, cache_write_tokens = :cw_tok, "
                "units = :units, unit_kind = :unit_kind, model = COALESCE(model, :model), "
                "credits_charged = :credits, "
                "billing_kind = CAST(:billing_kind AS generation_billing_kind), "
                "ledger_tx_id = :tx, latency_ms = :latency, stop_reason = :stop_reason, "
                "provider_ref = :provider_ref, meta = CAST(:meta AS JSONB), "
                "completed_at = now(), updated_at = now() "
                "WHERE id = :gid"
            ),
            {
                "gid": str(generation_id),
                "in_tok": usage.input_tokens,
                "out_tok": usage.output_tokens,
                "cr_tok": usage.cache_read_tokens,
                "cw_tok": usage.cache_write_tokens,
                "units": usage.units,
                "unit_kind": usage.unit_kind,
                "model": usage.model,
                "credits": credits_charged,
                "billing_kind": billing_kind,
                "tx": str(ledger_tx_id) if ledger_tx_id else None,
                "latency": latency_ms,
                "stop_reason": stop_reason,
                "provider_ref": provider_ref,
                "meta": json.dumps(meta),
            },
        )

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[GenerationRow]:
        # Every optional parameter is CAST explicitly: an untyped NULL used twice makes PostgreSQL
        # refuse the statement outright ("could not determine data type of parameter") — the
        # unfiltered list would 500. Found by exercising the endpoint, not by reading the code.
        rows = (
            await self._session.execute(
                text(
                    f"SELECT {_SELECT_COLUMNS} FROM generations WHERE user_id = :uid "
                    "AND (CAST(:kind AS text) IS NULL OR kind = CAST(:kind AS text)) "
                    "AND (CAST(:status AS text) IS NULL "
                    "     OR status = CAST(:status AS generation_status)) "
                    "ORDER BY created_at DESC LIMIT :n"
                ),
                {"uid": str(user_id), "kind": kind, "status": status, "n": limit},
            )
        ).all()
        return [_row(r) for r in rows]

    async def stats(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        """Reads the VIEW by name — swapping it for a materialized view later changes no API."""
        rows = (
            await self._session.execute(
                text(
                    "SELECT kind, generations, succeeded, failed, total_tokens, units, "
                    "credits_charged, last_generation_at FROM v_generations_user_totals "
                    "WHERE user_id = :uid ORDER BY kind"
                ),
                {"uid": str(user_id)},
            )
        ).all()
        return [
            {
                "kind": r[0],
                "generations": int(r[1] or 0),
                "succeeded": int(r[2] or 0),
                "failed": int(r[3] or 0),
                "totalTokens": int(r[4] or 0),
                "units": int(r[5] or 0),
                "creditsCharged": int(r[6] or 0),
                "lastGenerationAt": r[7],
            }
            for r in rows
        ]

    async def flip_trial(self, user_id: uuid.UUID) -> bool:
        """Consume the ONE lifetime trial — atomically, without locks.

        ``WHERE trial_used = FALSE`` makes two concurrent first generations resolve to exactly one
        trial: the loser updates zero rows and is charged normally.
        """
        result: Any = await self._session.execute(
            text("UPDATE users SET trial_used = TRUE WHERE id = :uid AND trial_used = FALSE"),
            {"uid": str(user_id)},
        )
        return bool(result.rowcount)

    async def count_inflight_global(self) -> dict[str, int]:
        """For the ``generations_inflight`` gauge (the same count the guard already needs)."""
        rows = (
            await self._session.execute(
                text(
                    "SELECT status, count(*) FROM generations "
                    "WHERE status IN ('pending','running') GROUP BY status"
                )
            )
        ).all()
        return {r[0]: int(r[1]) for r in rows}
