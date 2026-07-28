# Подключение I4 (перевод черновика) — блок

Модуль `translate.py` (I4) готов и покрыт (10 тестов), дрейф уже через
`result.changes` (решение №3 — ничего менять не надо). Остаётся:
1. параметр `source_language` в `translate_draft` (конфликт «черновик уже
   не всегда ru» — решение владельца «конфиг = источник»);
2. подключить I4 в `_handle_draft` после store;
3. событие `DRAFT_TRANSLATED`;
4. тесты подключения.

Тесты прогоняешь у себя.

---

## 1. Правка I4 — source_language параметром

`translate_draft` сейчас берёт источник из `config.source_language`
(хардкод "ru"). Черновик теперь генерится на языке панели → источник
должен приходить per-call. Параметр с дефолтом из конфига — обратно
совместимо, старые тесты не ломаются.

### Патч `app/drafts/translate.py`

**Сигнатура:**

НАЙТИ:
```python
    async def translate_draft(
        self, draft_id: str, draft_ru: str, target_language: str,
        *, fence: "Fence",
    ) -> str | None:
```
ЗАМЕНИТЬ НА:
```python
    async def translate_draft(
        self, draft_id: str, draft_ru: str, target_language: str,
        *, fence: "Fence", source_language: str | None = None,
    ) -> str | None:
```

**Эффективный источник (начало тела, где считается `src`):**

НАЙТИ:
```python
        # Normalize languages (lowercase, strip region)
        src = self._config.source_language.lower()
        tgt = target_language.lower()
```
ЗАМЕНИТЬ НА:
```python
        # Источник: параметр (язык генерации черновика из конфига сессии),
        # иначе дефолт конфига. «Конфиг = источник» — решение владельца.
        effective_source = source_language or self._config.source_language
        # Normalize languages (lowercase, strip region)
        src = effective_source.lower()
        tgt = target_language.lower()
```

**В сборке TranslationRequest:**

НАЙТИ:
```python
        req = TranslationRequest(
            text=draft_ru,
            source_language=self._config.source_language,  # keep original for provider
            target_language=target_language,  # keep original for provider
```
ЗАМЕНИТЬ НА:
```python
        req = TranslationRequest(
            text=draft_ru,
            source_language=effective_source,   # язык генерации черновика
            target_language=target_language,
```

Дрейф, empty-check, snapshot, режим LIVE_LITERAL — НЕ трогаем.

---

## 2. Подключение в `_handle_draft` (main.py)

### 2a. Создать транслятор в `start()` (рядом с DraftProvider)
```python
        from app.drafts.translate import DraftTranslator
        self._draft_translator = DraftTranslator(self._provider, self.draft_guard)
```

### 2b. В `_handle_draft`, ПОСЛЕ успешного store и UI-события DRAFT_CREATED

Вставить перевод. Целевой язык — `candidate.target_language` (положен I2
из meeting-потока). Источник — `cfg.generate_language`.

```python
        # I4: перевод черновика на язык встречи. Источник = язык генерации.
        # Недоступность перевода — ШТАТНЫЙ исход (контракт I4): черновик
        # остаётся на языке генерации, задачу не роняем.
        try:
            translated = await self._draft_translator.translate_draft(
                draft_id,
                candidate.draft_ru,
                candidate.target_language,
                fence=job.fence,
                source_language=cfg.generate_language,
            )
        except StaleGenerationError:
            translated = None                    # профиль сменился — перевод не клеим
        except ProviderError as exc:
            self.offline.mark_unavailable(provider.name, exc)
            translated = None                    # провайдер лёг — черновик и так полезен

        if translated is not None:
            await self.draft_guard.attach_translation(draft_id, translated)
            if self.ui_server:
                self.ui_server.publish(
                    EventType.DRAFT_TRANSLATED,
                    {"draft_id": draft_id, "draft_translated": translated},
                )
```

> Важно: перевод НЕ роняет DRAFT-задачу при `ProviderError`. Это осознанное
> отличие от TRANSLATE-обработчика: там перевод — единственный продукт, а
> здесь черновик уже сохранён и ценен сам. Контракт I4 прямо: «при
> недоступном текстовом провайдере черновик остаётся на языке генерации —
> штатный исход, не ошибка». Поэтому глотаем на уровне обработчика.
> `translate_draft` при этом свой контракт держит (пробрасывает
> ProviderError) — решение «не падать» принято ЗДЕСЬ, в обработчике.

> `translated is None` также при: совпадении языков (generate==target,
> перевод не нужен), дрейфе (lost_entity), пустом переводе. Во всех
> случаях `attach` не зовётся, событие не шлётся — корректно.

---

## 3. Тесты подключения (`tests/test_draft_translate_wiring.py`)

- **успех:** meeting-вопрос, generate=ru, target=en → fake-провайдер
  вернул перевод → `attach_translation` вызван 1 раз, событие
  DRAFT_TRANSLATED опубликовано, `draft_answers.draft_translated`
  заполнено;
- **дрейф:** перевод потерял число (`lost_entity` в changes) →
  `translate_draft` вернул None → `attach` НЕ вызван, события нет,
  черновик остался без перевода;
- **совпадение языков:** generate=en, target=en → перевод не запрашивался
  (провайдер не вызван), `attach` не вызван;
- **source_language параметр:** вызов с `source_language="en"` →
  `req.source_language == "en"` (перехват запроса к провайдеру);
- **ProviderError при переводе:** провайдер бросил → задача НЕ упала,
  черновик сохранён без `draft_translated`, `mark_unavailable` вызван.

Критерий: полный набор зелёный, число названо (ожидание: 297 + новые).

---

## 4. Точки сверки
| Что | Где |
| :-- | :-- |
| `self._draft_translator` не конфликтует с именами | main.py |
| `candidate.target_language` заполнен (I2 кладёт из req) | provider.py |
| `EventType.DRAFT_TRANSLATED` значение | server.py §7.1 |
| `cfg.generate_language` доступен в `_handle_draft` (тот же cfg, что для DraftRequest) | main.py |
| `ProviderError`/`StaleGenerationError` импортированы в main.py | шапка (уже есть от TRANSLATE) |

---

## Порядок
1. Правка I4 (§1) — параметр, тесты I4 не ломаются (дефолт).
2. Создание транслятора + подключение (§2).
3. Тесты (§3), прогон, число.

После этого цепочка черновиков полная: **STT → I3 → DRAFT-обработчик →
I2 → I5 → I4 → draft_answers(draft_ru + draft_translated)**. Останется
только E4 — вкладка UI (визуализация готовых данных).
