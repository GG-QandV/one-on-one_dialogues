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

from dataclasses import replace

from app.drafts.guardrails import DraftCandidate
from app.drafts.library import LibraryContext
from app.drafts.langcheck import check_language
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
    #: Дефолт, ЕСЛИ панель не передала. Реальный язык — из
    #: DraftRequest.generate_language (цепочка клиент→юзер→en разрешается
    #: в панели ДО вызова I2).
    default_language: str = "en"


# --------------------------------------------------------------------- request

@dataclass(frozen=True, slots=True)
class DraftRequest:
    session_id: str
    trigger_segment_id: str
    question_text: str          # реплика клиента (raw_text)
    target_language: str        # язык встречи — для последующего I4
    library_section_id: str     # активный раздел (I1)
    #: Итоговый язык генерации из панели (клиент→юзер→en уже разрешён).
    generate_language: str = "en"
    #: Тон из панели: пресет + свободное уточнение.
    tone_preset: str = "neutral"
    tone_note: str = ""


# --------------------------------------------------------------------- provider

class DraftProvider:
    """Генератор черновиков. Использует TranslationProvider как LLM-транспорт.

    Генерация — через translate с режимом DRAFT: инструкция в системном
    промпте (D4), в req.text — только данные (справка + вопрос).
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
            "lang_retry": 0,
            "lang_failed": 0,
        }

    async def generate(
        self, req: DraftRequest, *, fence: Fence
    ) -> DraftCandidate | None:
        """Сгенерировать черновик-кандидат. None = генерировать нечего.

        fence приходит ОТ вызывающего (обработчик JobType.DRAFT). require()
        внутри НЕ вызывается — тот же инвариант, что base.py и I4.
        """
        candidate = await self._generate_once(req, fence, strict_lang=False)
        if candidate is None:
            return None

        # Языковая проверка + 1 retry (решение: скрипт + n-gram, иначе флаг).
        candidate = await self._verify_language(candidate, req, fence)

        self._snapshot["generated"] += 1
        return candidate

    # ------------------------------------------------------- single generation

    async def _library_text(self, req: DraftRequest) -> str | None:
        if not req.question_text or not req.question_text.strip():
            self._snapshot["empty_question"] += 1
            return None
        try:
            ctx = await self._library.get(req.library_section_id)
        except Exception:  # noqa: BLE001
            self._snapshot["empty_library"] += 1
            return None
        text = ctx.content_text
        if not text or not text.strip():
            self._snapshot["empty_library"] += 1
            return None
        return text

    async def _generate_once(
        self, req: DraftRequest, fence: Fence, *, strict_lang: bool
    ) -> DraftCandidate | None:
        library_text = await self._library_text(req)
        if library_text is None:
            return None
        prompt = self._build_prompt(library_text, req.question_text)
        if strict_lang:
            prompt = (
                f"[ВАЖНО: отвечай строго на языке {req.generate_language}, "
                f"это критично]\n\n" + prompt
            )
        lang = req.generate_language or self._config.default_language
        llm_request = TranslationRequest(
            text=prompt,
            source_language=lang,
            target_language=lang,
            mode=TranslationMode.DRAFT,
            context=(),
            segment_id=None,
            tone_preset=req.tone_preset,
            tone_note=req.tone_note,
        )
        result = await self._provider.translate(llm_request, fence=fence)
        candidate = self._parse(result.translation_raw, req)
        if candidate is None:
            self._snapshot["parse_failed"] += 1
        return candidate

    # ------------------------------------------------------- language verify

    async def _verify_language(
        self, candidate: DraftCandidate, req: DraftRequest, fence: Fence
    ) -> DraftCandidate:
        v = check_language(candidate.draft_ru, req.generate_language)
        if v.ok:
            return candidate

        self._snapshot["lang_retry"] += 1
        second = await self._generate_once(req, fence, strict_lang=True)
        if second is None:
            return candidate                      # отдать первый как есть

        v2 = check_language(second.draft_ru, req.generate_language)
        if v2.ok:
            return second
        self._snapshot["lang_failed"] += 1
        return replace(second, lang_ok=False)     # флаг для UI

    # ------------------------------------------------------------- prompt

    def _build_prompt(self, library_text: str, question: str) -> str:
        # Только данные. Инструкция генерации — в системном промпте
        # режима DRAFT (D4). Здесь НЕТ инструкций модели — иначе снова
        # промпт в промпте.
        return (
            f"СПРАВКА:\n{library_text}\n\n"
            f"ВОПРОС СОБЕСЕДНИКА:\n{question}"
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

        answer = data.get("draft_ru")          # было "answer" (новый контракт)
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

        conf = data.get("confidence")
        confidence = float(conf) if isinstance(conf, (int, float)) else None
        sugg = data.get("suggested_clarification")
        suggested = str(sugg) if isinstance(sugg, str) and sugg.strip() else None

        return DraftCandidate(
            session_id=req.session_id,
            trigger_segment_id=req.trigger_segment_id,
            draft_ru=answer.strip(),
            target_language=req.target_language,
            sources=sources,
            has_gaps_claimed=has_gaps_claimed,
            gap_note=gap_note,
            confidence=confidence,
            suggested_clarification=suggested,
        )

    # ------------------------------------------------------------- snapshot

    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot)
