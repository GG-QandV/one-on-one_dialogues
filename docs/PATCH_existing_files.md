# Патч-блоки для существующих файлов (I2/I5 + тесты)

Применять в репозитории. Каждый блок — точная замена before→after.
Новые файлы (миграция, langcheck.py, prompt_loader.py, prompt_draft_mode.md)
уже готовы в комплекте `apply/` — их просто скопировать в дерево.

Прогон тестов — на твоей стороне (в моей среде исполняемого проекта нет).

---

## A. `app/drafts/guardrails.py`

### A1. DraftCandidate — +3 поля

НАЙТИ:
```python
@dataclass(frozen=True, slots=True)
class DraftCandidate:
    """То, что вернул DraftProvider (I2) до проверки."""

    session_id: str
    trigger_segment_id: str
    draft_ru: str
    target_language: str
    sources: tuple[str, ...]
    has_gaps_claimed: bool
    gap_note: str | None
```
ЗАМЕНИТЬ НА:
```python
@dataclass(frozen=True, slots=True)
class DraftCandidate:
    """То, что вернул DraftProvider (I2) до проверки."""

    session_id: str
    trigger_segment_id: str
    draft_ru: str
    target_language: str
    sources: tuple[str, ...]
    has_gaps_claimed: bool
    gap_note: str | None
    #: Уверенность для случая B/C (умозаключение). None = факт (случай A).
    #: Служебное поле: видно в UI, в текст черновика НЕ попадает.
    confidence: float | None = None
    #: Рекомендуемый вопрос к собеседнику при пробеле. None = уточнять нечего.
    suggested_clarification: str | None = None
    #: Язык ответа прошёл проверку. False = после retry язык всё ещё мимо.
    lang_ok: bool = True
```

### A2. GuardConfig — мягкий режим по умолчанию (решение №2)

НАЙТИ:
```python
    #: reject вместо accept_gaps при числах вне библиотеки. Для переговоров
    #: о деньгах строгий режим — рекомендуемый.
    strict_numbers: bool = True
```
ЗАМЕНИТЬ НА:
```python
    #: reject вместо accept_gaps при числах вне библиотеки.
    #: Решение владельца №2: мягкий режим по умолчанию. Строгий режим —
    #: явно в конфиге (UI-кнопка «перед стартом» отложена в tier 1.2).
    strict_numbers: bool = False
```

### A3. store — писать 3 новых поля

В методе `store`, в INSERT в `draft_answers`:

НАЙТИ (список колонок и VALUES — сверить с фактическим текстом):
```python
                "draft_ru, target_language, sources_json, has_gaps, gap_note, "
                "status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
```
ЗАМЕНИТЬ НА:
```python
                "draft_ru, target_language, sources_json, has_gaps, gap_note, "
                "confidence, suggested_clarification, lang_ok, "
                "status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
```

И в кортеже параметров добавить перед `now` (или перед статусом):
```python
                 candidate.confidence,
                 candidate.suggested_clarification,
                 int(candidate.lang_ok),
```

> ВНИМАНИЕ: точный текст INSERT в store я не видел дословно. Выше — форма.
> Сверить порядок плейсхолдеров с фактическим SQL перед применением.

`verify` НЕ меняется: confidence/suggested_clarification/lang_ok на вердикт
не влияют, это UI-поля.

---

## B. `app/drafts/provider.py`

### B1. DraftProviderConfig — язык не хардкод

НАЙТИ:
```python
    #: Язык генерации черновика. Всегда русский (спека §12).
    generate_language: str = "ru"
```
ЗАМЕНИТЬ НА:
```python
    #: Дефолт, ЕСЛИ панель не передала. Реальный язык — из
    #: DraftRequest.generate_language (цепочка клиент→юзер→en разрешается
    #: в панели ДО вызова I2).
    default_language: str = "en"
```

### B2. DraftRequest — +язык и тон

НАЙТИ:
```python
@dataclass(frozen=True, slots=True)
class DraftRequest:
    session_id: str
    trigger_segment_id: str
    question_text: str          # реплика клиента (raw_text)
    target_language: str        # язык встречи — для последующего I4
    library_section_id: str     # активный раздел (I1)
```
ЗАМЕНИТЬ НА:
```python
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
```

### B3. generate — язык из запроса + языковая проверка

НАЙТИ:
```python
        llm_request = TranslationRequest(
            text=prompt,                        # СПРАВКА + ВОПРОС (см. _build_prompt)
            source_language=self._config.generate_language,
            target_language=self._config.generate_language,  # генерация на ru, не перевод
            mode=TranslationMode.DRAFT,         # отдельный промпт, без вложенности
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
```
ЗАМЕНИТЬ НА:
```python
        candidate = await self._generate_once(req, fence, strict_lang=False)
        if candidate is None:
            return None

        # Языковая проверка + 1 retry (решение: скрипт + n-gram, иначе флаг).
        candidate = await self._verify_language(candidate, req, fence)

        self._snapshot["generated"] += 1
        return candidate

    # ------------------------------------------------------- single generation

    async def _generate_once(
        self, req: DraftRequest, fence: Fence, *, strict_lang: bool
    ) -> DraftCandidate | None:
        library_text = await self._library_text(req)   # см. B3a
        if library_text is None:
            return None
        prompt = self._build_prompt(library_text, req.question_text)
        if strict_lang:
            prompt = (
                f"[ВАЖНО: отвечай строго на языке {req.generate_language}, "
                f"это критично]\n\n" + prompt
            )
        llm_request = TranslationRequest(
            text=prompt,
            source_language=req.generate_language,
            target_language=req.generate_language,
            mode=TranslationMode.DRAFT,
            context=(),
            segment_id=None,
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
        from dataclasses import replace
        from app.drafts.langcheck import check_language

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
```

> `_generate_once` использует помощник `_library_text(req)`, который
> инкапсулирует текущие шаги 1–2 из `generate` (пустой вопрос → None,
> библиотека целиком → None при пустой). Ниже B3a.

### B3a. Вынести загрузку библиотеки в помощник

Текущие «шаг 1» (пустой вопрос) и «шаг 2» (библиотека) из `generate`
переносятся в отдельный метод, чтобы retry их переиспользовал:

ДОБАВИТЬ метод:
```python
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
```

### B4. _parse — answer→draft_ru + служебные поля

НАЙТИ:
```python
        answer = data.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return None
```
ЗАМЕНИТЬ НА:
```python
        answer = data.get("draft_ru")          # было "answer" (новый контракт)
        if not isinstance(answer, str) or not answer.strip():
            return None
```

НАЙТИ (конец _parse, возврат кандидата):
```python
        return DraftCandidate(
            session_id=req.session_id,
            trigger_segment_id=req.trigger_segment_id,
            draft_ru=answer.strip(),
            target_language=req.target_language,
            sources=sources,
            has_gaps_claimed=has_gaps_claimed,
            gap_note=gap_note,
        )
```
ЗАМЕНИТЬ НА:
```python
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
```

### B5. snapshot — +счётчики языковой проверки

В инициализации `self._snapshot` добавить ключи:
```python
            "lang_retry": 0,
            "lang_failed": 0,
```

### B6. Системный промпт из загрузчика

Там, где D4/prompts собирает системный промпт для `TranslationMode.DRAFT`,
подключить `DraftPromptTemplate.build_system(req.generate_language,
req.tone_preset, req.tone_note)` вместо §12-константы. Библиотека
остаётся кэш-префиксом ПОСЛЕ системного промпта.

> Точка сверки: где именно D2/D3 берут system-часть для DRAFT-режима.
> Подставлять язык/тон нужно в system, до библиотечного префикса, чтобы
> не инвалидировать кэш справки.

---

## C. Тесты

### C1. `tests/test_provider_I2.py`

`_good_response()` — поле answer→draft_ru + новые поля:
```python
def _good_response():
    return (
        '{"draft_ru": "Работаю с Traefik.", '
        '"sources": ["Прайс-лист"], "has_gaps": false, "gap_note": null, '
        '"confidence": null, "suggested_clarification": null}'
    )
```

Все `DraftRequest(...)` — добавить `generate_language`, где тест ждёт
конкретный язык. Например тест, ждущий русский:
```python
    req = DraftRequest("s1", "seg1", "Сколько?", "en", "lib1",
                       generate_language="ru")
```

Новые тесты (добавить):
- parse `confidence` и `suggested_clarification` из ответа модели;
- языковая проверка: провал L1 → retry → успех (2 вызова провайдера);
- двойной провал → `lang_ok=False` в кандидате.

### C2. `tests/test_draft_guardrails.py`

Добавить тест на мягкий дефолт:
```python
    def test_default_soft_accepts_gapped_number(self):
        guard = DraftGuard.__new__(DraftGuard)
        guard._cfg = GuardConfig()          # дефолт теперь strict_numbers=False
        candidate = DraftCandidate(
            session_id="s1", trigger_segment_id="seg1",
            draft_ru="ориентировочно 50000 рублей",
            target_language="ru", sources=("lib1",),
            has_gaps_claimed=True, gap_note="цена не в справке",
        )
        verdict = guard.verify(candidate, "стандартная цена 30000 рублей")
        assert verdict.kind is VerdictKind.ACCEPT_WITH_GAPS
```
`test_rejects_unverified_numbers_strict` — НЕ трогать (задаёт
`strict_numbers=True` явно).

---

## D. Порядок применения

1. `migrations/0007_draft_confidence.sql` → в `migrations/`.
2. `langcheck.py`, `prompt_loader.py`, `prompt_draft_mode.md` → в `app/drafts/`.
3. Патчи A (guardrails), B (provider) по блокам выше.
4. Патч B6 — подключить загрузчик в D4/prompts (сверить место).
5. Патчи C (тесты).
6. Прогнать `test_provider_I2.py` + `test_draft_guardrails.py` вместе,
   назвать число зелёных.
7. В CONTRACTS зафиксировать: отклонение §12↔промпт (язык-параметр),
   мягкий дефолт strict_numbers, UI-кнопка строгого режима → tier 1.2.

## E. Точки сверки при применении (не решения — проверки по коду)

| Что | Где |
| :-- | :-- |
| Дословный INSERT в `store` | guardrails.py store |
| Схема `draft_answers` (имена колонок) | migrations + store |
| Где D4/prompts отдаёт DRAFT system-промпт | prompts.py |
| Дословный `_good_response()` | test_provider_I2.py |
| Номер миграции (0007 vs факт) | migrations/ |
