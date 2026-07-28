# Правки волны 1 по итогам верификации по коду

Верификация читала **код**, не `VERIFY_wave1.md` (который писан по памяти
о задании и пометил `ok` реальные дефекты). Ниже — что чинить, по
приоритету. Блокеры — до H1. Сложное (fence) уже разобрано ниже готовым
решением, остальное — точечные правки исполнителю.

**Правило:** `VERIFY_wave1.md` не источник — он недостоверен. Источник —
контракт и код. После правок verify переписать по факту кода.

---

## БЛОКЕР 1 — fence-перезахват (base.py + I4, одна причина)

### Суть

`base.py::translate` принимает `fence` параметром, но делает
`fence = self._privacy.require(...)`, затирая переданный. I4 усугубляет:
сам захватывает fence изнутри и передаёт в provider. Итог: fence,
захваченный вызывающим (обработчик `JobType.TRANSLATE`) в момент
постановки задачи, **игнорируется**. Защита от переключения профиля между
постановкой и выполнением (критерий §21.7) не работает.

### Правка base.py (ГОТОВАЯ — применить как есть)

В `app/translation/base.py`, метод `translate`, заменить блок проверки
приватности:

**Было:**

```python
if not self._privacy.allows(Capability.TEXT_TO_CLOUD):
    raise PrivacyViolation(Capability.TEXT_TO_CLOUD.value, self._privacy.profile.value)

# Захватываем fence ПЕРЕД вызовом (require — атомарная проверка + захват)
fence = self._privacy.require(Capability.TEXT_TO_CLOUD)
```

**Стало:**

```python
# fence приходит СНАРУЖИ — от вызывающего (обработчик JobType.TRANSLATE),
# который захватил его в момент постановки задачи. Перезахватывать нельзя:
# это потеряло бы переключение профиля, случившееся между постановкой и
# выполнением. Проверяем право по текущему профилю, но fence НЕ трогаем.
if not self._privacy.allows(Capability.TEXT_TO_CLOUD):
    raise PrivacyViolation(
        Capability.TEXT_TO_CLOUD.value, self._privacy.profile.value
    )
# fence используется как передан — валидация ниже, после _call, сверит
# его поколение с текущим и бросит StaleGenerationError при расхождении.
```

Остальное в `translate` (таймаут, `validate(fence, ...)` после `_call`,
`_parse`, аудит) — без изменений. `validate` уже стоит после `_call` —
это подтверждено, оно верно.

### Правка I4 (ГОТОВАЯ — применить как есть)

В `app/drafts/translate.py`, метод `translate_draft`, убрать
самостоятельный захват fence. I4 не владелец приватности — fence должен
приходить от вызывающего (обработчик `JobType.DRAFT`).

**Проблема:** текущая сигнатура `translate_draft(draft_id, draft_ru,
target_language)` не принимает fence. Значит правка — сигнатурная.

**Стало (сигнатура + тело):**

```python
async def translate_draft(
    self, draft_id: str, draft_ru: str, target_language: str,
    *, fence: "Fence",
) -> str | None:
    ...
    # УБРАТЬ полностью блок:
    #   from app.privacy import Capability
    #   fence = self._provider.privacy.require(Capability.TEXT_TO_CLOUD)
    # fence теперь приходит параметром.

    try:
        result = await self._provider.translate(req, fence=fence)
    except Exception:
        raise
```

Импорт `Fence`:

```python
from app.privacy import Fence
```

Вызывающий (обработчик `JobType.DRAFT` в main.py/queue) захватывает fence
`provider.privacy.require(Capability.TEXT_TO_CLOUD)` **до** постановки
задачи перевода черновика и передаёт в `translate_draft(..., fence=fence)`.
Это тот же паттерн, что для `JobType.TRANSLATE`.

### Тест, которого не хватает (оба модуля)

Добавить: fence захвачен со старым поколением, профиль переключён **до**
`translate`/`translate_draft`, `validate` бросает `StaleGenerationError`.
Существующий `test_switch_during_call` переключает профиль ВНУТРИ `_call` —
это не тот сценарий, перезахват его не ломал, потому он и «проходил».

---

## БЛОКЕР 2 — D2 Gemini: finishReason и blockReason не проверяются

### Суть

Контракт D2 п. 6: `finishReason != STOP` → `ProviderResponseInvalid`.
Контракт D2 п. 9: `promptFeedback.blockReason` → `ProviderResponseInvalid`.
В коде `_call` **ни того, ни другого нет** — проверяются только наличие
`candidates`/`content`/`parts`. Обрезанный ответ (`MAX_TOKENS`) и
блокировка фильтром пройдут как валидные.

### Правка (исполнителю — точечная)

В `gemini_text.py::_call`, после `data = resp.json()` и **до** извлечения
candidates:

```python
# Блокировка фильтром безопасности: 200 с promptFeedback.blockReason
prompt_feedback = data.get("promptFeedback") or {}
block_reason = prompt_feedback.get("blockReason")
if block_reason:
    raise ProviderResponseInvalid(f"blocked: {block_reason}")
```

После извлечения `candidate = candidates[0]`, до чтения content:

```python
finish_reason = candidate.get("finishReason")
if finish_reason and finish_reason != "STOP":
    raise ProviderResponseInvalid(f"finish_reason: {finish_reason}")
```

Оба сообщения — без `req.text` и без ответа (только код причины).

---

## БЛОКЕР 3 — D3 Claude: stop_reason не проверяется, `{` не восстановлена

### Суть

Контракт D3 п. 6: `stop_reason == "max_tokens"` → `ProviderResponseInvalid`.
Контракт D3 п. 5 + ловушка: при предзаполнении ассистента `{` ответ
приходит **без** открывающей скобки — надо склеить, иначе `json.loads`
падает на каждом `post_clean`. В коде `_call` возвращает
`"".join(text_parts).strip()` — `stop_reason` не читается, `{` не
добавляется. Каждый post_clean через Claude сломается.

### Правка (исполнителю — точечная)

В `claude_text.py::_call`, после `data = resp.json()`:

```python
stop_reason = data.get("stop_reason")
if stop_reason == "max_tokens":
    raise ProviderResponseInvalid("stop_reason: max_tokens")
```

После сборки `raw_text` из блоков, для POST_CLEAN восстановить скобку:

```python
raw_text = "".join(text_parts).strip()
if req.mode == TranslationMode.POST_CLEAN:
    # предзаполнили ассистента '{', ответ приходит без неё — вернуть
    if not raw_text.startswith("{"):
        raw_text = "{" + raw_text
return raw_text
```

Отказ модели (п. 10, `stop_reason: end_turn` + маркеры) — отдельная
проверка; если её нет, добавить по контракту. Список маркеров держать
коротким (ложное срабатывание дороже пропуска).

### Проверить D3-c: импорт dataclass

`ClaudeConfig` декорирован `@dataclass(frozen=True)`, но в импортах
`claude_text.py` не видно `from dataclasses import dataclass`. Проверить:
если импорта нет — модуль не грузится, и D3 никогда не тестировался живьём
(verify сам пишет: взаимозаменяемость с D2 «не проверялось напрямую»).
Если отсутствует — добавить:

```python
from dataclasses import dataclass
```

---

## НЕ-БЛОКЕРЫ (долг, чинить после блокеров)

### D2-c: POST_CLEAN без responseSchema

Контракт D2 п. 5 + подсказки: `responseMimeType` + **`responseSchema`**.
В коде только `responseMimeType`. Добавить схему из подсказок контракта D2
(объект с `clean_text`/`changes`). Без неё структура не гарантирована, но
`validate_response` частично страхует — потому не блокер.

### I4-b: убрать двойной detect_drift ИЛИ зафиксировать

Контракт I4 ловушка: аудит уже прогнан в base.py, находки в
`result.changes` — читать их, не звать `detect_drift` повторно. Код зовёт
повторно. Заменить:

```python
# было:
drift = detect_drift(req, result.translation_raw)
if self._config.reject_on_drift and drift:
    ...
# стало:
lost = [c for c in result.changes if c.type == "lost_entity"]
if self._config.reject_on_drift and lost:
    self._snapshot["rejected_drift"] += 1
    return None
```

Это убирает лишний вызов И делает I4 независимым от того, вызывается ли
detect_drift снаружи. Убрать импорт `detect_drift`, если больше не нужен.

### D4: дубль SKU-блока в detect_drift

В `prompts.py::detect_drift` два идентичных цикла
`for sku in _extract_skus(...)` подряд. Удалить второй. Безвредно, но
мусор.

### G1: тест идемпотентности фильтра

Код `redactor.filter` сам отмечает риск двойного счёта при повторном
вызове. Добавить тест: одна запись через фильтр дважды → маскировка
корректна, счётчик не задваивается ложно. Обработка `exc_info` (формат +
обнуление) — верна, это подтверждено, не трогать.

---

## ПОСЛЕ ПРАВОК — переписать VERIFY_wave1.md

Текущий недостоверен: пометил `ok` блокеры 2, 3 и I4-a, описал код,
которого нет (D3 «добавляется }», D2 «добавлен responseSchema», I4
«читает result.changes»). Переписать **по коду после правок**, желательно
другим агентом, не автором кода. Формат прежний, но каждый `ok` — с
цитатой строки, доказывающей соответствие, а не пересказом задания.

---

## Порядок

```
1. base.py fence (готовая правка) ──┐
2. I4 fence (готовая правка)        ├─ блокеры приватности, вместе
3. D2 finishReason/blockReason      │
4. D3 stop_reason/{                 ├─ блокеры ответа
5. D3 проверить импорт dataclass    ┘
   ── прогнать тесты, добавить тест switch-до-translate ──
6. D2 responseSchema                ┐
7. I4 двойной drift                 ├─ долг
8. D4 дубль SKU                     │
9. G1 идемпотентность               ┘
10. переписать VERIFY_wave1.md по коду
```

Блокеры 1–5 — до H1: без них перевод либо течёт по приватности (1),
либо принимает обрезанные/заблокированные ответы (2,3), либо падает на
post_clean (3). Долг 6–9 — после, до объявления волны 1 закрытой.
