"""Limits, plan (тариф), data deletion, and the temporary file links."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.deps import DbSession
from app.domain import access
from app.domain.errors import FileLinkInvalidError
from app.domain.models import Source, Topic
from app.domain.pricing import KIND_ANSWER, KIND_SUMMARY, DomainPricing
from app.domain.routers.deps import Settings, UserId
from app.domain.schemas import AccountDataDeletion, LimitsView, PlanProductView, PlanView
from app.domain.sources import get_storage
from app.domain.storage import verify_link
from app.domain.topics import soft_delete_topics
from app.models import Subscription
from app.products import get_products

router = APIRouter(prefix="/v1", tags=["Account"])
files_router = APIRouter(prefix="/v1/files", tags=["Files"])

_FEATURES = [
    "pdf_text_layer",
    "pasted_text",
    "web_article",
    "summary_with_citations",
    "chat_with_citations",
]


@router.get(
    "/limits",
    response_model=LimitsView,
    summary="Форматы и лимиты",
    description="Показывайте до отправки файла. Ровно эти значения проверяет сервер.",
)
async def limits(user_id: UserId, session: DbSession, settings: Settings) -> LimitsView:
    used = await session.scalar(
        select(func.coalesce(func.sum(Source.file_size), 0)).where(
            Source.user_id == user_id, Source.deleted_at.is_(None)
        )
    )
    return LimitsView(
        pdf={
            "mimeTypes": ["application/pdf"],
            "maxBytes": settings.pdf_max_bytes,
            "maxPages": settings.pdf_max_pages,
            "textLayerRequired": True,
            "ocr": False,
        },
        text={"minChars": settings.text_min_chars, "maxChars": settings.text_max_chars},
        web={"maxBytes": settings.web_max_bytes, "pagesPerLink": 1},
        topic={"maxSources": settings.topic_max_sources},
        user={
            "maxTopics": settings.user_max_topics,
            "storageMaxBytes": settings.user_storage_max_bytes,
            "storageUsedBytes": int(used or 0),
        },
        question={"maxChars": settings.question_max_chars},
    )


@router.get(
    "/plan",
    response_model=PlanView,
    summary="Тариф и остаток",
    description=(
        "Статус доступа, баланс, стоимость операций и продукты с ценой/периодом из конфигурации "
        "(`PLAN_DISPLAY`). Доступ подтверждает только сервер: возврат со страницы оплаты ничего "
        "не меняет, пока платёж не подтверждён вебхуком."
    ),
)
async def plan(user_id: UserId, session: DbSession, settings: Settings) -> PlanView:
    answer_policy = await access.check(session, user_id, KIND_ANSWER)
    summary_policy = await access.check(session, user_id, KIND_SUMMARY)
    sub = await session.scalar(select(Subscription).where(Subscription.user_id == user_id))
    pricing = DomainPricing()
    display = settings.plan_display()
    products = []
    for product in get_products().values():
        shown = display.get(product.product_id, {})
        products.append(
            PlanProductView(
                productId=product.product_id,
                kind=product.kind,
                credits=product.credits,
                title=product.title,
                channels=sorted(product.channels),
                price=shown.get("price"),
                currency=shown.get("currency"),
                period=shown.get("period"),
            )
        )
    links = {
        k: v
        for k, v in {
            "support": settings.support_url,
            "privacy": settings.privacy_url,
            "terms": settings.terms_url,
        }.items()
        if v
    }
    return PlanView(
        subscriptionStatus=answer_policy.subscription_status.value,
        plan=sub.plan if sub else None,
        expiresAt=sub.expires_at if sub else None,
        creditsBalance=answer_policy.credits_balance,
        trialAvailable=settings.trial_enabled and not answer_policy.trial_used,
        prices={
            "answer": pricing.quote(kind=KIND_ANSWER, model=None, params={}),
            "summary": pricing.quote(kind=KIND_SUMMARY, model=None, params={}),
            "summaryExtraPart": settings.summary_credits_per_extra_part,
            "summaryPartChars": settings.summary_batch_chars,
            "summarySinglePassChars": settings.summary_single_pass_chars,
        },
        canAsk=answer_policy.allowed,
        canSummarize=summary_policy.allowed,
        blockReason=access.block_reason(answer_policy),
        demoFreeQuestionsLeft=await access.demo_free_left(session, user_id, settings),
        products=products,
        features=_FEATURES,
        links=links,
    )


@router.delete(
    "/account/data",
    response_model=AccountDataDeletion,
    summary="Удалить мои данные",
    description=(
        "Удаляет все темы, материалы, файлы, индексы, историю и конспекты (сразу недоступны, "
        "физически — через `purgeAfterSeconds`). Платёжные записи сохраняются как учётные данные."
    ),
)
async def delete_my_data(
    user_id: UserId, session: DbSession, settings: Settings
) -> AccountDataDeletion:
    ids = list(
        (
            await session.scalars(
                select(Topic.id).where(Topic.user_id == user_id, Topic.deleted_at.is_(None))
            )
        ).all()
    )
    await soft_delete_topics(session, ids, user_id, settings)
    return AccountDataDeletion(
        topicsScheduled=len(ids), purgeAfterSeconds=settings.purge_delay_seconds
    )


@files_router.get(
    "/{token}",
    summary="Скачать оригинал по временной ссылке",
    description="Без JWT: доступ даёт подписанный токен. Удалённый материал — `404`.",
    response_class=FileResponse,
)
async def download(token: str, session: DbSession, settings: Settings) -> FileResponse:
    claims = verify_link(settings.files_signing_secret, token)
    if claims is None:
        raise FileLinkInvalidError("Ссылка недействительна или устарела.")
    row = (
        await session.execute(
            select(Source.file_key, Source.file_mime, Source.original_filename, Source.title)
            .join(Topic, Topic.id == Source.topic_id)
            .where(
                Source.id == claims.source_id,
                Source.user_id == claims.user_id,
                Source.deleted_at.is_(None),
                Topic.deleted_at.is_(None),
            )
        )
    ).first()
    storage = get_storage()
    if row is None or not row[0] or not storage.exists(row[0]):
        raise FileLinkInvalidError("Файл недоступен.")
    filename = row[2] or f"{row[3][:80]}.{_ext(row[1])}"
    return FileResponse(
        storage.path(row[0]),
        media_type=row[1] or "application/octet-stream",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, max-age=60",
        },
    )


def _ext(mime: str | None) -> str:
    if mime and "pdf" in mime:
        return "pdf"
    if mime and "html" in mime:
        return "html"
    return "txt"
