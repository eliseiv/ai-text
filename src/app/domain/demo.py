"""The demo topic: shows the value (summary → question → answer with a citation → source) BEFORE
any payment. Explicitly marked (``isDemo``), read-only, provisioned once per user.

Its summary is pre-written, but its citations go through the SAME ``CitationResolver`` as model
output — a demo quote that is not a real substring fails provisioning (and the unit test), so the
demo can never show a citation the product would reject.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.citations import CitationResolver, CitationTable, VerificationStats
from app.domain.models import Chunk, Source, SourceVersion, Summary, Topic, UserDomainState
from app.domain.storage import LocalFileStorage, sha256_hex, source_key
from app.domain.text import chunk_text

DEMO_TOPIC_TITLE = "Демо: как устроена память"
DEMO_SOURCE_TITLE = "Почему мы забываем и как это исправить"

DEMO_TEXT = """\
Почему мы забываем и как это исправить

В 1885 году немецкий психолог Герман Эббингауз опубликовал работу «О памяти». Он заучивал \
наборы бессмысленных слогов и через разные промежутки времени проверял, сколько запомнил. \
Результат получил название «кривая забывания»: большая часть нового материала теряется в первые \
часы и дни, а затем забывание замедляется.

Эббингауз заметил и обратное: каждое повторение делает забывание более медленным. Если \
повторять материал через увеличивающиеся промежутки времени, для прочного запоминания нужно \
меньше усилий, чем при повторении подряд. Этот эффект называют эффектом интервалов, а метод \
обучения на его основе — интервальными повторениями.

Второй важный приём — активное припоминание. Попытка самостоятельно вспомнить ответ, а не \
перечитать конспект, укрепляет память сильнее, чем пассивное повторение. В исследованиях этот \
эффект называют эффектом тестирования: проверочные вопросы работают не только как контроль, но \
и как способ обучения.

Третий приём — чередование тем. Когда задачи разных типов перемешаны, учиться сложнее, зато \
навык различать, какой подход применить, развивается лучше, чем при решении однотипных задач \
подряд.

Практический вывод простой: разбейте материал на вопросы, отвечайте на них по памяти и \
возвращайтесь к ним через день, через несколько дней и через неделю. Такой режим требует меньше \
времени, чем многократное перечитывание, и даёт более устойчивый результат.
"""

DEMO_THESES: tuple[dict[str, Any], ...] = (
    {
        "text": "Большая часть нового материала забывается в первые часы и дни, затем забывание "
        "замедляется — это «кривая забывания» Эббингауза (1885).",
        "quotes": [
            "большая часть нового материала теряется в первые часы и дни, а затем забывание "
            "замедляется",
        ],
    },
    {
        "text": "Повторения через увеличивающиеся интервалы дают прочное запоминание с меньшими "
        "усилиями, чем повторение подряд.",
        "quotes": [
            "Если повторять материал через увеличивающиеся промежутки времени, для прочного "
            "запоминания нужно меньше усилий, чем при повторении подряд",
        ],
    },
    {
        "text": "Самостоятельное припоминание укрепляет память сильнее, чем перечитывание; "
        "проверочные вопросы — это тоже способ учиться.",
        "quotes": [
            "Попытка самостоятельно вспомнить ответ, а не перечитать конспект, укрепляет память "
            "сильнее, чем пассивное повторение",
            "проверочные вопросы работают не только как контроль, но и как способ обучения",
        ],
    },
    {
        "text": "Чередование разных типов задач усложняет обучение, но лучше развивает умение "
        "выбирать подход.",
        "quotes": [
            "навык различать, какой подход применить, развивается лучше, чем при решении "
            "однотипных задач подряд",
        ],
    },
)

DEMO_QUESTIONS = (
    "Что такое кривая забывания?",
    "Как часто нужно повторять материал?",
    "Почему самопроверка эффективнее перечитывания?",
)


def build_demo_summary(
    chunks: list[dict[str, Any]], versions: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    resolver = CitationResolver(chunks, versions)
    stats = VerificationStats()
    table = CitationTable()
    theses = []
    for thesis in DEMO_THESES:
        claims = resolver.verify_claims(
            [
                {
                    "text": thesis["text"],
                    "citations": [
                        {"chunk": chunks[0]["key"], "quote": q} for q in thesis["quotes"]
                    ],
                }
            ],
            stats,
        )
        if not claims or len(claims[0].citations) != len(thesis["quotes"]):
            raise RuntimeError("demo summary quote does not match the demo text")
        idx = [table.add(c) for c in claims[0].citations]
        theses.append({"text": thesis["text"], "citations": [i + 1 for i in idx]})
    total = sum(len(c["text"]) for c in chunks)
    version_id, version = next(iter(versions.items()))
    return {
        "theses": theses,
        "questions": list(DEMO_QUESTIONS),
        "citations": table.to_json(),
        "coverage": {
            "totalChars": total,
            "coveredChars": total,
            "partial": False,
            "sources": [
                {
                    "sourceId": version["source_id"],
                    "versionId": version_id,
                    "title": version["title"],
                    "totalChars": total,
                    "coveredChars": total,
                }
            ],
        },
        "skippedSources": [],
    }


async def ensure_demo(session: AsyncSession, user_id: uuid.UUID, storage: LocalFileStorage) -> None:
    """Provision the demo topic once. Deleting it is final (the flag stays set)."""
    claimed = await session.scalar(
        pg_insert(UserDomainState)
        .values(user_id=user_id, demo_provisioned=True)
        .on_conflict_do_nothing(index_elements=["user_id"])
        .returning(UserDomainState.user_id)
    )
    if claimed is None:
        return
    text = DEMO_TEXT.strip()
    topic_id = await session.scalar(
        insert(Topic)
        .values(user_id=user_id, title=DEMO_TOPIC_TITLE, is_demo=True)
        .returning(Topic.id)
    )
    source_id = uuid.uuid4()
    data = text.encode("utf-8")
    key = source_key(user_id, source_id, "original.txt")
    storage.put(key, data)
    await session.execute(
        insert(Source).values(
            id=source_id,
            topic_id=topic_id,
            user_id=user_id,
            kind="text",
            title=DEMO_SOURCE_TITLE,
            status="ready",
            file_key=key,
            file_mime="text/plain; charset=utf-8",
            file_size=len(data),
            content_sha256=sha256_hex(data),
            char_count=len(text),
        )
    )
    version_id = await session.scalar(
        insert(SourceVersion)
        .values(
            source_id=source_id,
            user_id=user_id,
            version_no=1,
            text=text,
            char_count=len(text),
            text_sha256=sha256_hex(data),
            pages=None,
            meta={"demo": True},
        )
        .returning(SourceVersion.id)
    )
    spans = chunk_text(text)
    chunk_ids = [uuid.uuid4() for _ in spans]
    await session.execute(
        insert(Chunk),
        [
            {
                "id": cid,
                "version_id": version_id,
                "source_id": source_id,
                "user_id": user_id,
                "ordinal": s.ordinal,
                "start": s.start,
                "end": s.end,
                "text": s.text,
            }
            for s, cid in zip(spans, chunk_ids, strict=True)
        ],
    )
    await session.execute(
        update(Source).where(Source.id == source_id).values(current_version_id=version_id)
    )
    chunks = [
        {
            "key": f"c{s.ordinal + 1}",
            "chunk_id": str(cid),
            "version_id": str(version_id),
            "ordinal": s.ordinal,
            "start": s.start,
            "end": s.end,
            "text": s.text,
        }
        for s, cid in zip(spans, chunk_ids, strict=True)
    ]
    versions = {
        str(version_id): {
            "source_id": str(source_id),
            "title": DEMO_SOURCE_TITLE,
            "kind": "text",
            "pages": None,
        }
    }
    await session.execute(
        insert(Summary).values(
            topic_id=topic_id,
            user_id=user_id,
            status="succeeded",
            requested_source_ids=[source_id],
            source_version_ids=[version_id],
            content=build_demo_summary(chunks, versions),
            usage={"creditsCharged": 0, "free": True},
            completed_at=select(Topic.created_at).where(Topic.id == topic_id).scalar_subquery(),
        )
    )


__all__ = ["DEMO_TEXT", "ensure_demo", "build_demo_summary"]
