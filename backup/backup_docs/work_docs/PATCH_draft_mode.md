# Патчи: режим DRAFT (правило «нет промпта в промпте») + правка имён I1

Причина: I2 генерировал черновик через `POST_CLEAN` — промпт генерации
вкладывался в промпт чистки (вложенный промпт). Архитектурное правило это
запрещает. Решение — отдельный режим `DRAFT` с собственным промптом: один
промпт, без вложенности.

Плюс правка имён под фактический API I1 (`get`, не `get_text`).

Порядок применения строгий: D1 → D4 → I2. Каждый шаг — после предыдущего.

---

## ПАТЧ 1 — `app/translation/base.py` (D1): добавить режим

В `class TranslationMode`:

```python
class TranslationMode(str, Enum):
    LIVE_LITERAL = "live_literal"
    LIVE_SAFE = "live_safe"
    POST_CLEAN = "post_clean"
    DRAFT = "draft"              # <-- добавить: генерация черновика ответа
```

Больше в base.py ничего. `translate()` режим не хардкодит — прокидывает в
`_call`/`_parse`, промпт даёт D4. Существующие тесты D1 не затрагиваются.

---

## ПАТЧ 2 — `app/translation/prompts.py` (D4): промпт режима DRAFT

### 2.1 В `build()`, добавить ветку режима

Найти цепочку `if mode == LIVE_LITERAL / elif LIVE_SAFE / else POST_CLEAN`.
Добавить ветку DRAFT **перед** `else POST_CLEAN` (чтобы POST_CLEAN
остался явным последним):

```python
    elif mode == TranslationMode.DRAFT:
        # Промпт генерации черновика ответа. НЕ переводческий и НЕ чистящий —
        # отдельный первый промпт (правило: нет промпта в промпте).
        # Плейсхолдеры библиотеки/вопроса/лимита подставляет вызывающий (I2)
        # через параметры req, здесь — только каркас инструкции.
        system = (
            "Ты помогаешь менеджеру ответить на вопрос собеседника во время "
            "деловых переговоров. Используй ТОЛЬКО факты из справки.\n"
            "Ответь на языке справки, кратко.\n"
            "Используй только числа, суммы, сроки и условия из справки.\n"
            "Если данных для ответа в справке нет — верни \"{gap_marker}\" "
            "в поле answer и пустой список sources. НЕ придумывай числа и факты.\n"
            "В sources укажи ТОЧНЫЕ заголовки использованных разделов справки.\n"
            "Верни СТРОГО JSON без пояснений:\n"
            "{\"answer\": \"...\", \"sources\": [\"...\"], "
            "\"has_gaps\": false, \"gap_note\": null}"
        )
```

### 2.2 Подстановка `{gap_marker}` — как в POST_CLEAN, через `replace`

Промпт DRAFT содержит JSON-скобки (`{"answer": ...}`), поэтому `.format()`
на нём упадёт — та же ловушка, что в POST_CLEAN. В блоке подстановки
языков расширить условие:

```python
    # было:
    if mode == TranslationMode.POST_CLEAN:
        system = system.replace("{source_language}", source_lang_name)
        system = system.replace("{target_language}", target_lang_name)
    else:
        system = system.format(...)

    # стало:
    if mode in (TranslationMode.POST_CLEAN, TranslationMode.DRAFT):
        system = system.replace("{source_language}", source_lang_name)
        system = system.replace("{target_language}", target_lang_name)
        if mode == TranslationMode.DRAFT:
            system = system.replace("{gap_marker}", "нет данных")
    else:
        system = system.format(
            source_language=source_lang_name,
            target_language=target_lang_name,
        )
```

DRAFT языковые плейсхолдеры не использует, но `replace` на отсутствующем
плейсхолдере безвреден — оставить в общей ветке для единообразия.

### 2.3 `user_prompt` для DRAFT — библиотека + вопрос

Текущий `build` кладёт в user `format_context` + «Текст для перевода:».
Для DRAFT нужен другой user: справка + вопрос. Найти сборку user_parts,
добавить ветку:

```python
    if mode == TranslationMode.DRAFT:
        # req.text = собранный вызывающим блок "СПРАВКА:\n...\n\nВОПРОС:\n..."
        # I2 кладёт библиотеку и вопрос в req.text целиком.
        user = req.text
    else:
        user_parts = []
        if req.context:
            user_parts.append(format_context(req.context))
        user_parts.append(f"Текст для перевода:\n{req.text}")
        user = "\n\n".join(user_parts)
```

### 2.4 `validate_response` для DRAFT

DRAFT возвращает JSON (как POST_CLEAN), но структура иная
(`answer`/`sources`/`has_gaps`). Разбор структуры — задача I2 (`_parse`),
не D4. Здесь `validate_response` для DRAFT возвращает `translation_raw`
как есть (сырой JSON), без разбора:

```python
    if mode == TranslationMode.DRAFT:
        # Сырой JSON отдаётся I2, он разбирает его в DraftCandidate.
        # D4 структуру черновика не знает — только пробрасывает.
        return TranslationResult(translation_raw=raw.strip())
```

### 2.5 `audit` для DRAFT — не применять переводческий аудит

`detect_drift`/`filler_changes` — про перевод, к черновику неприменимы
(нет «оригинала» для сверки). Убедиться, что `audit` на режиме DRAFT не
падает и не дописывает переводческие changes. Если `audit` вызывается
безусловно из base.translate — добавить ранний возврат для DRAFT:

```python
def audit(req, result):
    if req.mode == TranslationMode.DRAFT:
        return result           # черновик не аудируется как перевод
    ...
```

Тесты D4: добавить `test_build_draft` (промпт содержит инструкцию из
справки, JSON-каркас, gap_marker подставлен) и
`test_validate_response_draft` (сырой JSON проброшен).

---

## ПАТЧ 3 — `app/drafts/provider.py` (I2): использовать режим DRAFT

### 3.1 Убрать POST_CLEAN, поставить DRAFT

```python
    # было:
    llm_request = TranslationRequest(
        text=prompt,
        source_language=self._config.generate_language,
        target_language=self._config.generate_language,
        mode=TranslationMode.POST_CLEAN,   # <-- вложенный промпт, УБРАТЬ
        context=(),
        segment_id=None,
    )

    # стало:
    llm_request = TranslationRequest(
        text=prompt,                        # СПРАВКА + ВОПРОС (см. _build_prompt)
        source_language=self._config.generate_language,
        target_language=self._config.generate_language,
        mode=TranslationMode.DRAFT,         # <-- отдельный промпт, без вложенности
        context=(),
        segment_id=None,
    )
```

### 3.2 `_build_prompt` упрощается

Инструкция-каркас теперь в D4 (system-промпт режима DRAFT). I2 собирает
только **данные** — справку и вопрос, без инструкций:

```python
    def _build_prompt(self, library_text: str, question: str) -> str:
        # Только данные. Инструкция генерации — в системном промпте
        # режима DRAFT (D4). Здесь НЕТ инструкций модели — иначе снова
        # промпт в промпте.
        return (
            f"СПРАВКА:\n{library_text}\n\n"
            f"ВОПРОС СОБЕСЕДНИКА:\n{question}"
        )
```

Старый `_build_prompt` с правилами («Ответь на русском», «Верни JSON»…)
**удалить целиком** — эти правила переехали в D4 как системный промпт.

### 3.3 Правка имён библиотеки (I1 даёт `get`, не `get_text`)

`LibraryPort`:

```python
class LibraryPort(Protocol):
    async def get(self, context_id: str) -> "LibraryContext":
        ...
```

Импорт: `from app.drafts.library import LibraryContext`

В `generate`, шаг 2:

```python
    try:
        ctx = await self._library.get(req.library_section_id)
    except Exception:
        self._snapshot["empty_library"] += 1
        return None
    library_text = ctx.content_text
    if not library_text or not library_text.strip():
        self._snapshot["empty_library"] += 1
        return None
```

---

## ПАТЧ 4 — тесты I2 (`test_provider_I2.py`)

FakeLibrary → метод `get`, возвращает `LibraryContext`:

```python
from app.drafts.library import LibraryContext
from app.errors import InvariantViolation

class FakeLibrary:
    def __init__(self, text):
        self._text = text
        self.calls = []
    async def get(self, context_id):
        self.calls.append(context_id)
        if self._text is None:
            raise InvariantViolation("library_context_not_found")
        return LibraryContext(
            id=context_id, name="test", domain=None,
            content_text=self._text, token_estimate=10,
            updated_at="2025-01-01T00:00:00.000",
        )
```

FakeProvider: проверить, что `last_request.mode == TranslationMode.DRAFT`
(было POST_CLEAN). Добавить тест:

```python
async def test_uses_draft_mode_not_postclean():
    provider = FakeProvider(_good_response())
    lib = FakeLibrary("цена 30000.")
    i2 = DraftProvider(provider, lib)
    await i2.generate(DraftRequest("s","seg","Сколько?","en","l"),
                      fence=_fence(provider))
    assert provider.last_request.mode == TranslationMode.DRAFT
```

Тест `test_library_inserted_whole_into_prompt` — проверить, что справка в
`last_request.text` (теперь это `СПРАВКА:\n...` без инструкций).

---

## Проверка после всех патчей

```
pytest tests/test_base_D1.py tests/test_prompt_modes.py \
       tests/test_provider_I2.py -q
ruff check app/translation/base.py app/translation/prompts.py \
           app/drafts/provider.py
mypy --strict app/drafts/provider.py
```

Весь набор прогнать вместе, назвать число (было N → стало N+2 за новые
тесты D4 и I2). Регрессий в D1/D4/I4 быть не должно — DRAFT добавлен, не
изменяет существующие режимы.

## Границы

- Не трогать тексты LIVE_LITERAL/LIVE_SAFE/POST_CLEAN — §11 дословно.
- DRAFT-промпт — новый, не переформулировка существующих.
- I2 не содержит инструкций модели в `_build_prompt` — только данные.
- Приватность/fence в I2 не менять — fence параметром, require() нет.
- `audit` на DRAFT не дописывает переводческие changes.

## Что зафиксировать в INTERFACES.md

Добавить `DRAFT` в перечисление `TranslationMode` (§4.1) и строку: режим
DRAFT — генерация черновика, промпт в D4, разбор структуры в I2. Правило
«нет промпта в промпте» — в раздел принципов.
