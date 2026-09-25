"""``GenerationService.run()`` — the ORDER OF STEPS IS THE MODULE.

Every step is where it is for a concrete reason. Departing from the order breaks a money invariant,
and the code below is the only place the domain cannot get wrong — because it never sees it.

    1.   POLICY GATE      quote() → evaluate(required_credits) → blocked ⇒ HTTP 200 + blockReason
                          ⚠ NO row in `generations` (every row = the provider WAS called)
    1.5  INFLIGHT GUARD   resource fuse (connection pool / upstream budget) ⇒ 409 too_many_inflight
                          ⚠ NO row either. Best-effort, NOT a money invariant
    2.   ANCHOR           INSERT ... ON CONFLICT DO NOTHING RETURNING id, then **COMMIT**
    3.   PROVIDER         the only call to domain code. ProviderError ⇒ failed, 0 credits, 502
    4.   CHARGE + DEBIT   one transaction: charge() → consume() | trial-flip → finalize → audit

⚠️ **STEP 2 COMMITS BEFORE THE NETWORK CALL — and that is the OPPOSITE of PaymentsJournal.**
The same ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` appears in ``billing/payments.py`` layer 1,
where committing separately is FORBIDDEN. The pattern carries no transactional requirement by
itself: what comes NEXT decides. Here a network call lasting minutes comes next — holding a pooled
connection through it exhausts the pool and the whole service stops answering, including /health.
There a local ``wallet.grant()`` in the same DB comes next — splitting the transaction there means
a silently lost payment. **Do not unify them.**
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import (
    EVENT_GENERATION_FAILED,
    EVENT_GENERATION_SUCCEEDED,
    EVENT_POLICY_BLOCKED,
    AuditService,
)
from app.config import CoreSettings
from app.errors import (
    AlreadyInProgressError,
    InsufficientCreditsError,
    TooManyInflightError,
    UpstreamError,
)
from app.extensions.loader import load_registry
from app.generation.contract import (
    GenerationProvider,
    GenerationRequest,
    GenerationResult,
    GenerationUsage,
    PricingPolicy,
    ProviderError,
)
from app.generation.impact import (
    IMPACT_NONE,
    IMPACT_REVENUE_LOSS,
    IMPACT_UPSTREAM,
    block_impact,
    generation_impact,
)
from app.generation.repository import GenerationRow, GenerationsRepository
from app.observability.context import set_generation_id
from app.observability.logging import log_event
from app.observability.metrics import (
    blocked_requests_total,
    generation_credits_charged_total,
    generation_latency_seconds,
    generation_total,
    generation_units_total,
    generation_upstream_errors_total,
)
from app.policy.engine import BillingKind, evaluate
from app.policy.loader import apply_gates, load_policy_state
from app.wallet.service import WalletService

logger = logging.getLogger("app.generation.service")

BILLING_CREDITS = "credits"
BILLING_TRIAL = "trial"
BILLING_UNBILLED = "unbilled"

_META_TRUNCATED = {"truncated": True}


@dataclass(frozen=True)
class GenerationOutcome:
    """What the domain router turns into HTTP. ``blocked`` is a 200, not an error."""

    status: str  # succeeded | blocked
    generation_id: uuid.UUID | None = None
    block_reason: str | None = None
    output: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    credits_charged: int = 0
    new_balance: int | None = None
    idempotent_replay: bool = False


class GenerationService:
    def __init__(
        self,
        session: AsyncSession,
        repo: GenerationsRepository,
        wallet: WalletService,
        audit: AuditService,
        provider: GenerationProvider,
        pricing: PricingPolicy,
        settings: CoreSettings,
    ) -> None:
        self._session = session
        self._repo = repo
        self._wallet = wallet
        self._audit = audit
        self._provider = provider
        self._pricing = pricing
        self._settings = settings

    async def run(
        self,
        *,
        user_id: uuid.UUID,
        kind: str,
        params: dict[str, Any],
        model: str | None = None,
        idempotency_key: str | None = None,
        request_id: str = "",
    ) -> GenerationOutcome:
        key = idempotency_key or str(uuid.uuid4())

        # ═══ STEP 1 — POLICY GATE ═══════════════════════════════════════════════════════════
        # quote() FIRST: with units/tokens pricing, "balance > 0" is the wrong question. Balance 1
        # and a price of 3 must block BEFORE the upstream runs — otherwise the provider's real
        # money is spent and the debit then finds nothing.
        state = await load_policy_state(self._session, user_id)
        estimate = self._pricing.quote(kind=kind, model=model, params=params)
        decision = apply_gates(
            evaluate(state, required_credits=estimate), state, {"kind": kind, "model": model}
        )
        if not decision.allowed:
            reason = decision.block_reason.value if decision.block_reason else "policy_denied"
            impact = block_impact(reason, self._gate_impacts())
            blocked_requests_total.labels(reason=reason, impact=impact).inc()
            generation_total.labels(
                kind=kind, provider=self._provider.name, status="blocked", impact=impact
            ).inc()
            await self._audit.log(
                EVENT_POLICY_BLOCKED,
                session=self._session,
                user_id=user_id,
                payload={
                    "blockReason": reason,
                    "requiredCredits": estimate,
                    "balance": state.credits_balance,
                },
            )
            self._log_outcome(
                result="blocked", reason=reason, impact=impact, kind=kind, user_id=user_id
            )
            # ⚠ NO row in `generations`: every row means the provider was called. Otherwise the
            # analytics lie and `credits_charged = 0 OR status='succeeded'` stops being meaningful.
            return GenerationOutcome(status="blocked", block_reason=reason)

        # ═══ STEP 1.5 — INFLIGHT GUARD (resource fuse, not a money invariant) ═══════════════
        # The anchor protects against a repeat of ONE request. It does nothing against N DIFFERENT
        # parallel requests, each of which would take a pooled connection into a minutes-long
        # upstream call. Best-effort on purpose (a race is possible): making it strict would need
        # locking on the hot path for a resource fuse. 0 disables it.
        max_inflight = self._settings.generation_max_inflight_per_user
        if max_inflight > 0 and await self._repo.count_inflight(user_id) >= max_inflight:
            generation_total.labels(
                kind=kind, provider=self._provider.name, status="blocked", impact=IMPACT_NONE
            ).inc()
            self._log_outcome(
                result="rejected",
                reason="too_many_inflight",
                impact=IMPACT_NONE,
                kind=kind,
                user_id=user_id,
            )
            raise TooManyInflightError("too many generations in flight")

        # ═══ STEP 2 — IDEMPOTENCY ANCHOR, then COMMIT (see the module docstring) ════════════
        gen_id = await self._repo.anchor(
            user_id=user_id,
            kind=kind,
            provider=self._provider.name,
            model=model,
            idempotency_key=key,
        )
        replay_row: GenerationRow | None = None
        if gen_id is None:
            existing = await self._repo.get_by_key(user_id, key)
            if existing is None:  # pragma: no cover - the unique index guarantees a row
                raise AlreadyInProgressError("generation already in progress")
            if existing.status == "succeeded":
                replay_row = existing  # same key, already done → 0 credits, the previous result
            elif existing.status in ("pending", "running"):
                # A DIFFERENT 409 from too_many_inflight: here the result WILL exist — the client
                # should wait and fetch it by id.
                raise AlreadyInProgressError("generation already in progress")
            else:  # failed | canceled → retrying on the same key is allowed
                await self._repo.retry(existing.id)
                gen_id = existing.id
        await self._session.commit()  # ⚠ MANDATORY here: a network call comes next.

        if replay_row is not None:
            generation_total.labels(
                kind=kind, provider=self._provider.name, status="replayed", impact=IMPACT_NONE
            ).inc()
            self._log_outcome(
                result="replayed",
                reason="idempotent_replay",
                impact=IMPACT_NONE,
                kind=kind,
                user_id=user_id,
                generation_id=replay_row.id,
            )
            return GenerationOutcome(
                status="succeeded",
                generation_id=replay_row.id,
                output=replay_row.meta.get("output"),
                usage=_usage_view(replay_row),
                credits_charged=0,  # nothing is charged twice
                new_balance=await self._balance(user_id),
                idempotent_replay=True,
            )

        if gen_id is None:  # pragma: no cover - the branches above assign or raise
            # Not an assert: `python -O` strips asserts, and this guards a money path — proceeding
            # with no anchor id would mean a provider call whose debit has no idempotency key.
            raise AlreadyInProgressError("generation anchor is missing")
        generation_id = uuid.UUID(str(gen_id))
        set_generation_id(str(generation_id))

        # ═══ STEP 3 — THE PROVIDER (no DB transaction held) ═════════════════════════════════
        req = GenerationRequest(
            user_id=user_id,
            kind=kind,
            idempotency_key=key,
            params=params,
            model=model,
            request_id=request_id,
            deadline_s=self._settings.generation_timeout_seconds,
        )
        started = time.monotonic()
        try:
            result = await self._provider.generate(req)
        except ProviderError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            await self._repo.fail(generation_id, error_code=exc.code, error_message=str(exc))
            await self._audit.log(
                EVENT_GENERATION_FAILED,
                session=self._session,
                user_id=user_id,
                generation_id=generation_id,
                payload={"kind": kind, "provider": self._provider.name, "errorCode": exc.code},
            )
            await self._session.commit()
            generation_upstream_errors_total.labels(
                provider=self._provider.name,
                status_code=str(exc.status_code) if exc.status_code else "none",
                error_type=exc.provider_error_type or "unknown",
            ).inc()
            generation_total.labels(
                kind=kind, provider=self._provider.name, status="failed", impact=IMPACT_UPSTREAM
            ).inc()
            generation_latency_seconds.labels(
                kind=kind, provider=self._provider.name, model=model or "none"
            ).observe(latency_ms / 1000)
            self._log_outcome(
                result="failed",
                reason=exc.code,
                impact=IMPACT_UPSTREAM,
                kind=kind,
                user_id=user_id,
                generation_id=generation_id,
                latency_ms=latency_ms,
            )
            # ⚠ NO DEBIT (BR-7). And the DB CHECK would refuse one even if this code tried.
            raise UpstreamError("generation provider failed") from exc

        latency_ms = int((time.monotonic() - started) * 1000)

        # ═══ STEP 4 — PRICE, DEBIT, ACCOUNT (one transaction) ═══════════════════════════════
        # The price comes ONLY from the server-side maps × the provider's usage, capped. Never from
        # the client body, never from the provider's output (it must not price itself).
        credits = self._pricing.charge(kind=kind, model=model, usage=result.usage)
        billing_kind, charged, ledger_tx_id = await self._settle(
            user_id=user_id,
            generation_id=generation_id,
            credits=credits,
            decision_kind=decision.billing_kind,
        )
        await self._repo.finalize(
            generation_id,
            usage=result.usage,
            credits_charged=charged,
            billing_kind=billing_kind,
            ledger_tx_id=ledger_tx_id,
            latency_ms=latency_ms,
            stop_reason=result.stop_reason,
            provider_ref=result.provider_ref,
            meta=self._meta(result),
        )
        await self._audit.log(
            EVENT_GENERATION_SUCCEEDED,
            session=self._session,
            user_id=user_id,
            generation_id=generation_id,
            payload={
                "kind": kind,
                "provider": self._provider.name,
                "model": model,
                "creditsCharged": charged,
                "units": result.usage.units,
            },
        )
        await self._session.commit()

        impact = generation_impact(
            status="succeeded", credits_charged=charged, billing_kind=billing_kind, stuck=False
        )
        generation_total.labels(
            kind=kind, provider=self._provider.name, status="succeeded", impact=impact
        ).inc()
        generation_credits_charged_total.labels(kind=kind, provider=self._provider.name).inc(
            charged
        )
        generation_units_total.labels(kind=kind, unit_kind=result.usage.unit_kind).inc(
            result.usage.units
        )
        generation_latency_seconds.labels(
            kind=kind, provider=self._provider.name, model=model or "none"
        ).observe(latency_ms / 1000)
        if impact == IMPACT_REVENUE_LOSS:
            # The service delivered a result for FREE (an under-estimating quote() let it through
            # and the debit found no credits). Correct behaviour — and, until this label existed,
            # entirely invisible.
            log_event(
                logger,
                logging.WARNING,
                "generation_unbilled",
                generationId=str(generation_id),
                userId=str(user_id),
                kind=kind,
                creditsQuoted=credits,
            )
        self._log_outcome(
            result="succeeded",
            reason=billing_kind,
            impact=impact,
            kind=kind,
            user_id=user_id,
            generation_id=generation_id,
            latency_ms=latency_ms,
            credits=charged,
        )

        return GenerationOutcome(
            status="succeeded",
            generation_id=generation_id,
            output=result.output,
            usage=_usage_dict(result.usage),
            credits_charged=charged,
            new_balance=await self._balance(user_id),
        )

    # ------------------------------------------------------------------ internals

    async def _settle(
        self,
        *,
        user_id: uuid.UUID,
        generation_id: uuid.UUID,
        credits: int,
        decision_kind: BillingKind,
    ) -> tuple[str, int, uuid.UUID | None]:
        """Trial-flip or debit. Returns ``(billing_kind, credits_charged, ledger_tx_id)``."""
        # Atomic, race-free: two concurrent first generations produce exactly ONE trial; the loser
        # falls through to a normal debit rather than getting a second free run.
        if decision_kind is BillingKind.trial and await self._repo.flip_trial(user_id):
            return BILLING_TRIAL, 0, None

        if credits <= 0:
            # A priced-at-zero generation that the provider actually DELIVERED is the service
            # working for free — exactly what `unbilled` means. Labelling it `credits` would make
            # it indistinguishable from a paid success (`impact=none`) and hide it from
            # `GenerationUnbilled`, which is the whole point of that alert. Causes: a mis-configured
            # PRICING_UNITS rate, a provider reporting `units=0`, a flat price of 0.
            return BILLING_UNBILLED, 0, None

        try:
            consumed = await self._wallet.consume(
                user_id=user_id,
                amount=credits,
                # THE debit key. `generation_id` was created by the anchor BEFORE the provider ran,
                # so a retry of this request debits exactly once.
                idempotency_key=f"generation:{generation_id}",
                reason="generation",
                generation_id=generation_id,
                meta={"generationId": str(generation_id)},
            )
        except InsufficientCreditsError:
            # quote() under-estimated: the provider already delivered, and the user has no credits.
            # We do NOT take him negative (the CHECK forbids it anyway) and we do NOT withhold the
            # result he already produced — the service eats the cost. `unbilled` makes that visible
            # (impact=revenue_loss → GenerationUnbilled).
            await self._session.rollback()
            return BILLING_UNBILLED, 0, None
        return BILLING_CREDITS, credits, consumed.tx_id

    def _meta(self, result: GenerationResult) -> dict[str, Any]:
        """``output`` lands in ``meta`` ONLY below the threshold. THE CORE NEVER STORES BLOBS.

        An image domain would otherwise put megabytes of base64 here: the DB swells, backups become
        unliftable, and ``SELECT * FROM generations`` takes the service down. Heavy artifacts belong
        in the domain's own storage; the row keeps a link.
        """
        payload: dict[str, Any] = {"output": result.output}
        encoded = json.dumps(payload)
        if len(encoded.encode("utf-8")) > self._settings.generation_meta_max_bytes:
            return {
                **_META_TRUNCATED,
                "outputBytes": len(encoded.encode("utf-8")),
                "providerRef": result.provider_ref,
            }
        return payload

    def _gate_impacts(self) -> dict[str, str]:
        """Impacts declared by the domain's policy gates (R-OBS-5)."""
        declared: dict[str, str] = {}
        for gate in load_registry().policy_gates:
            declared.update(getattr(gate, "block_impacts", {}) or {})
        return declared

    async def _balance(self, user_id: uuid.UUID) -> int:
        balance, _ = await self._wallet.get_wallet(user_id)
        return balance

    @staticmethod
    def _log_outcome(
        *,
        result: str,
        reason: str | None,
        impact: str,
        kind: str,
        user_id: uuid.UUID,
        generation_id: uuid.UUID | None = None,
        latency_ms: int | None = None,
        credits: int | None = None,
    ) -> None:
        """One structured outcome per run, on every exit path. Allowlist only — `params` and
        `output` (user content) are NEVER logged."""
        log_event(
            logger,
            logging.WARNING if impact != IMPACT_NONE else logging.INFO,
            "generation_outcome",
            result=result,
            reason=reason,
            impact=impact,
            kind=kind,
            userId=str(user_id),
            generationId=str(generation_id) if generation_id else None,
            latencyMs=latency_ms,
            creditsCharged=credits,
        )


def _usage_dict(usage: GenerationUsage) -> dict[str, Any]:
    return {
        "model": usage.model,
        "units": usage.units,
        "unitKind": usage.unit_kind,
        "inputTokens": usage.input_tokens,
        "outputTokens": usage.output_tokens,
    }


def _usage_view(row: GenerationRow) -> dict[str, Any]:
    return {
        "model": row.model,
        "units": row.units,
        "unitKind": row.unit_kind,
        "totalTokens": row.total_tokens,
    }
