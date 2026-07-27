"""app/drafts/provider.py — I2 генератор черновиков ответов.

Граница модуля (см. CONTRACTS/I2 spec): I2 ПРОИЗВОДИТ DraftCandidate и
отдаёт его вызывающему (обработчик JobType.DRAFT), который прогоняет его
через DraftGuard.verify + DraftGuard.store (I5). I2 НЕ проверяет числа,
источники, пробелы и НЕ пишет в БД — это делает I5.

Поток: trigger(I3) → I2.generate → DraftCandidate → guard.verify → guard.store.
I2 отвечает только за первую стрелку.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.drafts.guardrails import DraftCandidate
from app.drafts.library import LibraryContext
from app.privacy import Fence
from app.translation.base import (
    TranslationMode,
    TranslationProvider,
    TranslationRequest,
)

# --------------------------------------------------------------------- library port

@runtime_checkable
class LibraryPort(Protocol):
    """Минимальный контракт библиотеки фактов (I1), нужный I2."""

    async def get(self, context_id: str) -> LibraryContext:
        ...


# --------------------------------------------------------------------- config

@dataclass(frozen=True, slots=True)
class DraftProviderConfig:
    #: Синхронно с GuardConfig.max_words — берётся из общего конфига (B1).
    max_words: int = 120
    #: Синхронно с GuardConfig.gap_marker.
    gap_marker: str = "нет данных"
    #: Prompt caching префикса библиотеки. Требует поддержки провайдером;
    #: при отсутствии — работает без кэша, gap фиксируется в snapshot.
    cache_library: bool = False
    #: Язык генерации черновика. Всегда русский (спека §12).
    generate_language: str = "ru"


# --------------------------------------------------------------------- request

@dataclass(frozen=True, slots=True)
class DraftRequest:
    session_id: str
    trigger_segment_id: str
    question_text: str          # реплика клиента (raw_text)
    target_language: str        # язык встречи — для последующего I4
    library_section_id: str     # активный раздел (I1)


# --------------------------------------------------------------------- provider

class DraftProvider:
    """Генератор черновиков. Использует TranslationProvider как LLM-транспорт.

    ВАЖНО про транспорт: base.py (D1) предоставляет только translate(req, *,
    fence). Отдельного generate-метода у провайдера НЕТ. Поэтому черновик
    генерируется через translate с режимом POST_CLEAN: текст запроса —
    это собранный промпт (библиотека + вопрос + инструкция вернуть JSON),
    а translation_raw ответа содержит JSON черновика, который мы парсим.

    Это осознанный компромисс под фактический API D1. Если в D2/D3 появится
    выделенный draft-метод с prompt caching — переключить _invoke_llm на него.
    """

    def __init__(
        self,
        provider: TranslationProvider,
        library: LibraryPort,
        config: DraftProviderConfig | None = None,
    ) -> None:
        self._provider = provider
        self._library = library
        self._config = config or DraftProviderConfig()
        self._snapshot: dict[str, int] = {
            "generated": 0,
            "empty_question": 0,
            "empty_library": 0,
            "parse_failed": 0,
            "cache_unsupported": 0,
        }

    async def generate(
        self, req: DraftRequest, *, fence: Fence
    ) -> DraftCandidate | None:
        """Сгенерировать черновик-кандидат. None = генерировать нечего.

        fence приходит ОТ вызывающего (обработчик JobType.DRAFT). require()
        внутри НЕ вызывается — тот же инвариант, что base.py и I4.
        """
        # 1. Пустой вопрос — генерировать нечего
        if not req.question_text or not req.question_text.strip():
            self._snapshot["empty_question"] += 1
            return None

        # 2. Библиотека целиком (без RAG, §12)
        try:
            ctx = await self._library.get(req.library_section_id)
        except Exception:  # noqa: BLE001
            self._snapshot["empty_library"] += 1
            return None
        library_text = ctx.content_text
        if not library_text or not library_text.strip():
            self._snapshot["empty_library"] += 1
            return None

        # 3. Собрать промпт: библиотека = стабильный префикс, вопрос = переменная
        prompt = self._build_prompt(library_text, req.question_text)

        # 4. Вызвать LLM через translate (фактический API D1)
        llm_request = TranslationRequest(
            text=prompt,
            source_language=self._config.generate_language,
            target_language=self._config.generate_language,  # генерация на ru, не перевод
            mode=TranslationMode.POST_CLEAN,   # ждём структурированный JSON-ответ
            context=(),
            segment_id=None,
        )

        # Провайдерские ошибки пробрасываем — обработчик DRAFT решит ретрай (queue).
        result = await self._provider.translate(llm_request, fence=fence)

        # 5. Разобрать ответ в кандидата
        candidate = self._parse(result.translation_raw, req)
        if candidate is None:
            self._snapshot["parse_failed"] += 1
            return None

        self._snapshot["generated"] += 1
        return candidate

    # ------------------------------------------------------------- prompt

    def _build_prompt(self, library_text: str, question: str) -> str:
        """Промпт генерации черновика.

        Требует от модели: ответ на русском ≤ max_words, список ТОЧНЫХ
        источников из справки, явную пометку gap_marker при отсутствии
        данных, запрет выдумывать числа. Структура ответа — строгий JSON.
        """
        gap = self._config.gap_marker
        return (
            "Ты помогаешь менеджеру ответить на вопрос собеседника во время "
            "деловых переговоров. Используй ТОЛЬКО факты из справки ниже.\n\n"
            "СПРАВКА:\n"
            f"{library_text}\n\n"
            "ВОПРОС СОБЕСЕДНИКА:\n"
            f"{question}\n\n"
            "ПРАВИЛА:\n"
            f"- Ответь на русском языке, не более {self._config.max_words} слов.\n"
            "- Используй только числа, суммы, сроки и условия из справки.\n"
            f"- Если данных для ответа в справке нет — верни \"{gap}\" в поле "
            "answer и пустой список sources. НЕ придумывай числа и факты.\n"
            "- В sources укажи ТОЧНЫЕ заголовки использованных разделов справки.\n\n"
            "Верни СТРОГО JSON без пояснений:\n"
            '{"answer": "...", "sources": ["..."], "has_gaps": false, '
            '"gap_note": null}'
        )

    # ------------------------------------------------------------- parse

    def _parse(self, raw: str, req: DraftRequest) -> DraftCandidate | None:
        """Разобрать JSON-ответ модели в DraftCandidate.

        I2 НЕ чистит числа, НЕ проверяет источники — только раскладывает
        ответ по полям кандидата. Проверка — работа I5.
        """
        text = raw.strip()
        # Снять возможную ```json-обёртку
        if text.startswith("```"):
            text = text.split("```", 2)[1] if "```" in text[3:] else text
            text = text.lstrip("json").lstrip("\n").strip()
            if text.endswith("```"):
                text = text[:-3].strip()

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

        answer = data.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return None

        sources_raw = data.get("sources") or []
        sources = tuple(
            str(s) for s in sources_raw if isinstance(s, (str, int, float))
        )

        gap_note = data.get("gap_note")
        if gap_note is not None:
            gap_note = str(gap_note)

        # has_gaps форсируется по ФАКТУ: если gap_marker в тексте — True,
        # независимо от того, что модель заявила в поле has_gaps (п. 6 спеки).
        marker_present = self._config.gap_marker.lower() in answer.lower()
        has_gaps_claimed = bool(data.get("has_gaps")) or marker_present

        return DraftCandidate(
            session_id=req.session_id,
            trigger_segment_id=req.trigger_segment_id,
            draft_ru=answer.strip(),
            target_language=req.target_language,
            sources=sources,
            has_gaps_claimed=has_gaps_claimed,
            gap_note=gap_note,
        )

    # ------------------------------------------------------------- snapshot

    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot)
