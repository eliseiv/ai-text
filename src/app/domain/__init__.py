"""The sources / AI-summary / Q&A-with-citations domain, plugged into the core via ``REGISTRY``.

What the domain adds (no core file is edited):

* routers — topics, sources (PDF / text / web), chat, summaries, limits/plan/account, file links;
* ``DomainSettings`` — LLM, limits, storage, worker, demo;
* ``DomainPricing`` — fixed credits per answer / summary (other kinds → the built-in policy);
* tables (migration ``0002``) and their truncation in tests;
* body-limit rules for the upload routes.

Generation runs in the worker (``python -m app.domain.worker``) through the core
``GenerationService.run()`` — see ``runs.py``.

``POST /v1/generate`` (the template's raw sample route) is mounted ONLY when ``ENVIRONMENT=dev``:
the core test-suite uses it as its harness for the billing invariants; in staging/prod it does not
exist. Read from ``os.environ`` on purpose — calling ``get_settings()`` here would recurse into
the registry loader while this module is still importing.
"""

from __future__ import annotations

import os

from app.domain.config import DomainSettings
from app.domain.models import DOMAIN_TABLES
from app.domain.pricing import DomainPricing
from app.domain.routers.account import files_router
from app.domain.routers.account import router as account_router
from app.domain.routers.chat import router as chat_router
from app.domain.routers.chat import summaries_router
from app.domain.routers.generate import router as generate_router
from app.domain.routers.sources import router as sources_router
from app.domain.routers.topics import router as topics_router
from app.extensions.registry import BodyLimitRule, DomainRegistry

_MB = 1024 * 1024


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Transport limit = the application limit + multipart overhead. Keeping them tied is the
# invariant from BodyLimitRule: the gateway must never cut below what /v1/limits advertises.
_PDF_BODY_LIMIT = _int_env("PDF_MAX_BYTES", 30 * _MB) + 1 * _MB
# Pasted text: up to TEXT_MAX_CHARS characters, worst case \\uXXXX-escaped JSON (6 bytes/char).
_TEXT_BODY_LIMIT = _int_env("TEXT_MAX_CHARS", 300_000) * 6 + 64 * 1024

_routers = [
    topics_router,
    sources_router,
    chat_router,
    summaries_router,
    account_router,
    files_router,
]
if os.environ.get("ENVIRONMENT", "dev") == "dev":
    _routers.append(generate_router)

REGISTRY = DomainRegistry(
    routers=tuple(_routers),
    openapi_tags=(
        {
            "name": "Topics",
            "description": "Темы пользователя: создать, найти, переименовать, удалить.",
        },
        {
            "name": "Sources",
            "description": "Материалы темы: PDF с текстовым слоем, вставленный текст, "
            "одна веб-статья. Обработка на сервере со статусами и повтором.",
        },
        {
            "name": "Chat",
            "description": "Вопросы по выбранным готовым материалам. "
            "Ответы с проверенными цитатами.",
        },
        {"name": "Summaries", "description": "AI-конспект с цитатами и готовыми вопросами."},
        {"name": "Account", "description": "Лимиты, тариф, удаление данных."},
        {"name": "Files", "description": "Временные ссылки на оригиналы."},
    ),
    api_description="""

### Асинхронная обработка
Импорт, ответы и конспекты выполняются фоновым воркером. Клиент получает объект со статусом и
опрашивает его по `id`; обработка продолжается, даже если приложение свёрнуто.

### Повтор без дублей
`POST`-запросы создания принимают `Idempotency-Key`: повтор после потери сети возвращает тот же
объект (`idempotentReplay: true`), без дубликата и повторного списания.

### Цитаты
Цитаты формирует и проверяет сервер: каждая — точный фрагмент версии источника со смещениями
(`offsets`) и, для PDF, физической и печатной страницей. Клиент не достраивает цитаты сам.
""",
    body_limit_rules=(
        BodyLimitRule(match="/v1/topics/*/sources/pdf", limit=_PDF_BODY_LIMIT, methods=("POST",)),
        BodyLimitRule(match="/v1/topics/*/sources/text", limit=_TEXT_BODY_LIMIT, methods=("POST",)),
    ),
    pricing_policy=DomainPricing(),
    settings_cls=DomainSettings,
    truncate_tables=DOMAIN_TABLES,
)
