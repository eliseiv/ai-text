"""API schemas of the domain. camelCase on the wire, like the core."""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import StrictModel

SourceKind = Literal["pdf", "text", "web"]
SourceStatus = Literal["uploading", "extracting", "indexing", "ready", "failed"]
RunStatus = Literal["queued", "running", "succeeded", "failed", "blocked", "canceled"]


class ErrorView(StrictModel):
    code: str
    message: str
    suggestion: str | None = Field(
        default=None,
        description="Что предложить пользователю: `paste_text` — вставить текст вручную, "
        "`retry` — повторить, `upgrade` — оформить доступ.",
    )


# --- topics ------------------------------------------------------------------------------------
class TopicCreate(StrictModel):
    title: str | None = Field(
        default=None,
        max_length=200,
        description="Пусто — сервер предложит название по первому готовому материалу.",
    )


class TopicUpdate(StrictModel):
    title: str = Field(min_length=1, max_length=200)


class TopicView(StrictModel):
    id: uuid.UUID
    title: str
    titleIsAuto: bool = Field(description="Название предложено сервером и ещё может обновиться.")
    isDemo: bool = Field(description="Демо-тема: только чтение, показывается с явной пометкой.")
    sourcesCount: int
    readySourcesCount: int
    createdAt: datetime.datetime
    updatedAt: datetime.datetime
    idempotentReplay: bool = False


class TopicsPage(StrictModel):
    items: list[TopicView]
    nextCursor: str | None = None


# --- sources -----------------------------------------------------------------------------------
class SourceTextCreate(StrictModel):
    title: str | None = Field(default=None, max_length=200)
    text: str = Field(min_length=1, description="Вставленный текст. Лимиты — `GET /v1/limits`.")


class SourceWebCreate(StrictModel):
    url: str = Field(min_length=8, max_length=2048)


class SourceUpdate(StrictModel):
    selected: bool | None = Field(default=None, description="Использовать в ответах и конспекте.")
    title: str | None = Field(default=None, min_length=1, max_length=200)


class SourceView(StrictModel):
    id: uuid.UUID
    topicId: uuid.UUID
    kind: SourceKind
    title: str
    status: SourceStatus = Field(
        description="`uploading` (загрузка) → `extracting` (извлечение текста) → `indexing` "
        "(подготовка к вопросам) → `ready` | `failed`. Процентов нет: обработка идёт на сервере "
        "и не зависит от того, открыто ли приложение."
    )
    selected: bool
    error: ErrorView | None = None
    url: str | None = None
    originalFilename: str | None = None
    fileSize: int
    pageCount: int | None = None
    charCount: int | None = None
    pagesWithoutText: list[int] = Field(
        default_factory=list, description="Страницы PDF без текстового слоя (не прочитаны)."
    )
    currentVersionId: uuid.UUID | None = None
    fetchedAt: str | None = Field(default=None, description="Веб: когда сделан снимок страницы.")
    createdAt: datetime.datetime
    updatedAt: datetime.datetime
    duplicate: bool = Field(default=False, description="Такой же материал уже есть в теме.")
    idempotentReplay: bool = False


class SourcesList(StrictModel):
    items: list[SourceView]
    readySelectedCount: int
    canAsk: bool = Field(description="Есть хотя бы один готовый выбранный материал.")


class PageView(StrictModel):
    number: int = Field(description="Физический номер страницы в файле (с 1).")
    label: str | None = Field(description="Печатный номер страницы, если он задан в PDF.")
    start: int
    end: int


class SourceContentView(StrictModel):
    sourceId: uuid.UUID
    versionId: uuid.UUID
    isCurrentVersion: bool
    kind: SourceKind
    title: str
    text: str = Field(description="Фрагмент текста версии `[offset, offset+len(text))`.")
    offset: int
    totalChars: int
    pages: list[PageView] = Field(default_factory=list, description="Только для PDF.")
    pagesWithoutText: list[int] = Field(default_factory=list)
    url: str | None = None
    fetchedAt: str | None = None


class OriginalLinkView(StrictModel):
    url: str = Field(description="Временная ссылка на оригинал (без токена авторизации).")
    expiresAt: datetime.datetime
    mime: str
    filename: str | None = None


# --- answers / citations -----------------------------------------------------------------------
class PageRefView(StrictModel):
    physical: int
    label: str | None = None


class OffsetsView(StrictModel):
    start: int
    end: int


class CitationView(StrictModel):
    index: int = Field(description="Номер сноски `[n]` в тексте ответа.")
    sourceId: uuid.UUID
    versionId: uuid.UUID
    chunkId: str | None = None
    quote: str = Field(description="Точный фрагмент оригинала (язык оригинала).")
    offsets: OffsetsView = Field(description="Смещения в тексте версии источника.")
    page: PageRefView | None = Field(default=None, description="PDF: страница начала цитаты.")
    pageEnd: PageRefView | None = None
    sourceTitle: str
    sourceKind: SourceKind
    sourceDeleted: bool = Field(default=False, description="Источник удалён — «Источник удалён».")


class PositionView(StrictModel):
    text: str
    citations: list[int]


class AnswerBlockView(StrictModel):
    type: Literal["claim", "conflict"]
    text: str
    citations: list[int] = Field(default_factory=list)
    positions: list[PositionView] = Field(default_factory=list)


class AnswerView(StrictModel):
    status: Literal["answered", "no_answer"]
    text: str
    blocks: list[AnswerBlockView]
    citations: list[CitationView]
    partialContext: bool = Field(
        description="Материалов больше, чем помещается в контекст: ответ построен по наиболее "
        "релевантным фрагментам."
    )


class UsedSourceView(StrictModel):
    sourceId: uuid.UUID
    versionId: uuid.UUID | None = None
    title: str
    deleted: bool = False


class SkippedSourceView(StrictModel):
    sourceId: uuid.UUID
    title: str
    reason: str = Field(description="`not_ready` | `failed` | `deleted` | `not_selected`")


class UsageView(StrictModel):
    creditsCharged: int = 0
    free: bool = Field(default=False, description="Бесплатно: пробный запрос или демо.")


class AskRequest(StrictModel):
    question: str = Field(min_length=1, max_length=4000)
    sourceIds: list[uuid.UUID] | None = Field(
        default=None,
        description="Явный выбор источников. Пусто — все готовые выбранные материалы темы.",
    )


class MessageView(StrictModel):
    id: uuid.UUID
    topicId: uuid.UUID
    question: str
    status: RunStatus = Field(
        description="`queued`/`running` — ждём ответ; `blocked` — нужен доступ (см. "
        "`blockReason`), после покупки вызовите `retry`; `canceled` — отменено, не списано."
    )
    answer: AnswerView | None = None
    sources: list[UsedSourceView] = Field(default_factory=list)
    sourceVersionIds: list[uuid.UUID] = Field(default_factory=list)
    skippedSources: list[SkippedSourceView] = Field(default_factory=list)
    blockReason: str | None = None
    error: ErrorView | None = None
    usage: UsageView = Field(default_factory=UsageView)
    createdAt: datetime.datetime
    completedAt: datetime.datetime | None = None
    idempotentReplay: bool = False


class MessagesPage(StrictModel):
    items: list[MessageView] = Field(description="Новые сверху.")
    nextCursor: str | None = None


# --- summaries ---------------------------------------------------------------------------------
class SummaryRequest(StrictModel):
    sourceIds: list[uuid.UUID] | None = None


class SummaryPriceView(StrictModel):
    credits: int = Field(
        description="Сколько кредитов спишется за конспект (0 — готовый уже есть)."
    )
    parts: int = Field(description="Сколько частей прочитает модель (1 — весь текст за раз).")
    partial: bool = Field(description="Материалов больше лимита: конспект покроет не весь текст.")
    reusedSummaryId: uuid.UUID | None = None


class ThesisView(StrictModel):
    text: str
    citations: list[int]


class CoverageSourceView(StrictModel):
    sourceId: uuid.UUID
    versionId: uuid.UUID
    title: str | None = None
    totalChars: int
    coveredChars: int


class CoverageView(StrictModel):
    totalChars: int
    coveredChars: int
    partial: bool = Field(description="Конспект построен не по всему тексту — показать пометку.")
    sources: list[CoverageSourceView]


class SummaryView(StrictModel):
    id: uuid.UUID
    topicId: uuid.UUID
    status: RunStatus
    theses: list[ThesisView] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, description="Готовые вопросы.")
    citations: list[CitationView] = Field(default_factory=list)
    coverage: CoverageView | None = None
    sources: list[UsedSourceView] = Field(default_factory=list)
    skippedSources: list[SkippedSourceView] = Field(default_factory=list)
    stale: bool = Field(
        default=False,
        description="Набор выбранных готовых материалов изменился после построения конспекта.",
    )
    reused: bool = Field(
        default=False, description="Конспект по тем же версиям уже был — не списано."
    )
    blockReason: str | None = None
    error: ErrorView | None = None
    usage: UsageView = Field(default_factory=UsageView)
    createdAt: datetime.datetime
    completedAt: datetime.datetime | None = None
    idempotentReplay: bool = False


# --- limits / plan / account -------------------------------------------------------------------
class LimitsView(StrictModel):
    pdf: dict[str, Any]
    text: dict[str, Any]
    web: dict[str, Any]
    topic: dict[str, Any]
    user: dict[str, Any]
    question: dict[str, Any]


class PlanProductView(StrictModel):
    productId: str
    kind: str
    credits: int
    title: str
    channels: list[str]
    price: str | None = None
    currency: str | None = None
    period: str | None = Field(default=None, description="ISO-8601 период, например `P1M`.")


class PlanView(StrictModel):
    subscriptionStatus: str
    plan: str | None = None
    expiresAt: datetime.datetime | None = None
    creditsBalance: int
    trialAvailable: bool
    prices: dict[str, int] = Field(description="Стоимость операций в кредитах.")
    canAsk: bool
    canSummarize: bool
    blockReason: str | None = None
    demoFreeQuestionsLeft: int
    products: list[PlanProductView]
    features: list[str]
    links: dict[str, str]


class AccountDataDeletion(StrictModel):
    topicsScheduled: int
    purgeAfterSeconds: int
