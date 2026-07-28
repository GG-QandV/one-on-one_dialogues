# Миграция I2/I5 под новый DRAFT-промпт — DIFF на ревью

Статус: **НЕ ПРИМЕНЕНО**. Это план. Применяю после твоего «go».

Решения, на которых построен diff (зафиксированы ранее):
- §12 спеки НЕ трогаем; промпт живёт отдельным конфигом (`prompt_draft_mode.md`).
- Язык генерации — параметр из панели (осознанное отклонение от «§12 always-ru»).
- Маркер вероятности → служебные поля `confidence`, `suggested_clarification`.
- `strict_numbers` → мягкий режим (дефолт `False`) — решение владельца №2.
- Проверка языка: скрипт + компактный n-gram детектор (0 зависимостей),
  1 retry → иначе флаг `lang_ok=False`.
- Строгий режим (`strict_numbers=True`) через UI-кнопку — отложен в
  TIER 1.2; сейчас только конфиг-флаг, мягкий дефолт.

Затронуто 6 файлов кода + 1 миграция БД + 2 файла тестов. Ниже — по каждому.

---

## Ограничения выполнения (честно, до diff)

1. **`_good_response()` в тестах I2 не прочитан целиком** — вижу, что
   `_parse` читает поле `answer`, значит фикстура даёт `answer`. Точный
   текст фикстуры сверю при применении; diff теста ниже — по факту
   переименования поля.
2. **Компактный язык-детектор — новая зависимость.** Нарушает lean-deps
   принцип. Ниже в §7 три варианта с трейд-оффами — нужен твой выбор, я
   не решаю сам.
3. **Схема `draft_answers`** (DDL) не прочитана дословно — миграция ниже
   добавляет 2 колонки, имена сверю с фактической таблицей.

---

## 1. `app/drafts/guardrails.py` (I5)

### 1.1 DraftCandidate — +2 поля

```diff
 @dataclass(frozen=True, slots=True)
 class DraftCandidate:
     session_id: str
     trigger_segment_id: str
     draft_ru: str
     target_language: str
     sources: tuple[str, ...]
     has_gaps_claimed: bool
     gap_note: str | None
+    #: Уверенность для случая B/C (умозаключение). None = факт (случай A).
+    #: Служебное поле: видно в UI, в текст черновика НЕ попадает.
+    confidence: float | None = None
+    #: Рекомендуемый вопрос к собеседнику при пробеле. None = уточнять нечего.
+    suggested_clarification: str | None = None
+    #: Язык ответа прошёл проверку. False = после retry язык всё ещё мимо,
+    #: UI показывает бейдж «язык под вопросом». Дефолт True — не ломает вызовы.
+    lang_ok: bool = True
```

Поля с дефолтами → существующие вызовы `DraftCandidate(...)` в тестах и
коде **не ломаются** (позиционные без новых полей продолжают работать).

### 1.2 GuardConfig — мягкий режим по умолчанию (решение №2)

```diff
 @dataclass(frozen=True, slots=True)
 class GuardConfig:
-    #: reject вместо accept_gaps при числах вне библиотеки. Для переговоров
-    #: о деньгах строгий режим — рекомендуемый.
-    strict_numbers: bool = True
+    #: reject вместо accept_gaps при числах вне библиотеки.
+    #: Решение владельца №2: мягкий режим по умолчанию — число вне
+    #: библиотеки при заявленном пробеле → ACCEPT_WITH_GAPS, не REJECT.
+    #: Строгий режим включается явно в конфиге для денежных переговоров.
+    strict_numbers: bool = False
     max_words: int = 120
     gap_marker: str = "нет данных"
```

⚠️ **Ломает тест** `test_rejects_unverified_numbers_strict` — он создаёт
`GuardConfig(strict_numbers=True)` явно, так что НЕ ломается. Но семантика
дефолта изменилась: код, полагавшийся на строгость «из коробки», теперь
получает мягкий. В main.py `DraftGuard` создаётся с мягким дефолтом.
UI-кнопка «строгий режим перед стартом» — TIER 1.2 (фронт + проброс через
сессию, заметный объём). Сейчас строгий доступен только через конфиг.

### 1.3 verify — confidence не влияет на вердикт

Никаких правок в логике `verify`. `confidence` и `suggested_clarification`
— UI-поля, на ACCEPT/REJECT не влияют. Единственное: `store` должен их
сохранить (ниже).

### 1.4 store — писать 2 новых поля

```diff
     async def store(self, candidate, verdict) -> str | None:
         ...
         def _tx(conn):
             conn.execute(
-                "INSERT INTO draft_answers (id, session_id, trigger_segment_id, "
-                "draft_ru, target_language, sources_json, has_gaps, gap_note, status, created_at) "
-                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
+                "INSERT INTO draft_answers (id, session_id, trigger_segment_id, "
+                "draft_ru, target_language, sources_json, has_gaps, gap_note, "
+                "confidence, suggested_clarification, lang_ok, status, created_at) "
+                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                 (..., candidate.confidence, candidate.suggested_clarification,
                  int(candidate.lang_ok), now),
             )
```

> Точные имена колонок/порядок сверить с фактическим `store`. Diff
> показывает форму, не финальный SQL.

---

## 2. Миграция БД — +2 колонки в draft_answers

Новый файл `migrations/00XX_draft_confidence.sql`:

```sql
ALTER TABLE draft_answers ADD COLUMN confidence REAL;
ALTER TABLE draft_answers ADD COLUMN suggested_clarification TEXT;
ALTER TABLE draft_answers ADD COLUMN lang_ok INTEGER NOT NULL DEFAULT 1;
```

confidence/suggested_clarification nullable, lang_ok с дефолтом 1 —
старые строки останутся валидны. Номер миграции — следующий
по порядку в `migrations/`.

---

## 3. `app/drafts/provider.py` (I2) — parse и запрос

### 3.1 DraftProviderConfig — язык больше не хардкод

```diff
 @dataclass(frozen=True, slots=True)
 class DraftProviderConfig:
     max_words: int = 120
     gap_marker: str = "нет данных"
     cache_library: bool = False
-    #: Язык генерации черновика. Всегда русский (спека §12).
-    generate_language: str = "ru"
+    #: Дефолтный язык, ЕСЛИ панель не передала. Реальный язык — из
+    #: DraftRequest.generate_language. Цепочка дефолта (клиент→юзер→en)
+    #: разрешается в панели ДО вызова I2, сюда приходит уже итог.
+    default_language: str = "en"
```

### 3.2 DraftRequest — +язык и тон из панели

```diff
 @dataclass(frozen=True, slots=True)
 class DraftRequest:
     session_id: str
     trigger_segment_id: str
     question_text: str
     target_language: str
     library_section_id: str
+    #: Итоговый язык генерации из панели (клиент→юзер→en уже разрешён).
+    generate_language: str = "en"
+    #: Тон из панели: пресет + свободное уточнение.
+    tone_preset: str = "neutral"
+    tone_note: str = ""
```

### 3.3 generate — язык из запроса, системный промпт из конфига

```diff
         llm_request = TranslationRequest(
             text=prompt,
-            source_language=self._config.generate_language,
-            target_language=self._config.generate_language,
+            source_language=req.generate_language,
+            target_language=req.generate_language,
             mode=TranslationMode.DRAFT,
             context=(),
             segment_id=None,
         )
-        result = await self._provider.translate(llm_request, fence=fence)
-        candidate = self._parse(result.translation_raw, req)
+        result = await self._provider.translate(llm_request, fence=fence)
+        candidate = self._parse(result.translation_raw, req)
+        # НОВОЕ: языковая проверка + 1 retry (см. §5)
+        candidate = await self._verify_language(candidate, req, fence)
```

### 3.4 _parse — answer→draft_ru, +2 поля, has_gaps по boolean

```diff
     def _parse(self, raw: str, req: DraftRequest) -> DraftCandidate | None:
         ...
-        answer = data.get("answer")
-        if not isinstance(answer, str) or not answer.strip():
+        answer = data.get("draft_ru")          # было "answer"
+        if not isinstance(answer, str) or not answer.strip():
             return None

         sources_raw = data.get("sources") or []
         sources = tuple(str(s) for s in sources_raw if isinstance(s, (str,int,float)))

         gap_note = data.get("gap_note")
         if gap_note is not None:
             gap_note = str(gap_note)

-        marker_present = self._config.gap_marker.lower() in answer.lower()
-        has_gaps_claimed = bool(data.get("has_gaps")) or marker_present
+        # Новый промпт даёт явный boolean has_gaps. Маркер-строка остаётся
+        # как страховка (модель могла забыть флаг) — двойная защита.
+        marker_present = self._config.gap_marker.lower() in answer.lower()
+        has_gaps_claimed = bool(data.get("has_gaps")) or marker_present
+
+        # НОВОЕ: служебные поля
+        conf = data.get("confidence")
+        confidence = float(conf) if isinstance(conf, (int, float)) else None
+        sugg = data.get("suggested_clarification")
+        suggested = str(sugg) if isinstance(sugg, str) and sugg.strip() else None

         return DraftCandidate(
             session_id=req.session_id,
             trigger_segment_id=req.trigger_segment_id,
             draft_ru=answer.strip(),
             target_language=req.target_language,
             sources=sources,
             has_gaps_claimed=has_gaps_claimed,
             gap_note=gap_note,
+            confidence=confidence,
+            suggested_clarification=suggested,
         )
```

> Имя поля `draft_ru` в JSON сохраняем (ты подтвердил «формат оставляем»),
> хотя содержимое теперь на языке панели. Менять имя поля на `draft` —
> отдельное решение, в этот diff НЕ входит.

---

## 4. Системный промпт DRAFT — загрузка из конфига

Сейчас: системный промпт DRAFT-режима собирается в `prompts.py` (D4) из
§11/§12. Наш новый промпт — внешний файл (`prompt_draft_mode.md`), с
подстановкой `{generate_language}`, `{tone_preset}`, `{tone_note}`.

Нужен загрузчик (в `prompts.py` или новый `app/drafts/prompt_loader.py`):

```python
def build_draft_system(generate_language: str, tone_preset: str,
                       tone_note: str) -> str:
    tmpl = _load_draft_template()          # из YAML/файла, hot-reload по mtime
    return tmpl.format(
        generate_language=generate_language,
        tone_preset=tone_preset or "neutral",
        tone_note=tone_note or "",
    )
```

Встраивается в `TranslationMode.DRAFT` ветку `prompts.build()` — там, где
сейчас берётся §12-текст. §12 спеки остаётся как есть (описывает намерение);
источник промпта для кода — конфиг.

> Точка сверки: как `prompts.build()` сейчас отдаёт DRAFT-промпт и куда
> провайдер D2/D3 кладёт системный промпт. Подставлять язык/тон нужно
> ДО кэш-префикса библиотеки (переменная часть промпта не должна ломать
> кэш справки — язык/тон идут в system ПЕРЕД библиотекой или помечаются
> вне кэша).

---

## 5. Языковая проверка + retry (новый код)

Новый модуль `app/drafts/langcheck.py`:

```python
@dataclass(frozen=True, slots=True)
class LangVerdict:
    ok: bool
    detected: str            # код языка или "script:cyrillic" при грубом
    reason: str

def check_language(text: str, expected: str) -> LangVerdict:
    # Уровень 1 — система письма (мгновенно, по Unicode-диапазонам):
    #   ожидаем кириллицу → латиница/CJK в теле = провал.
    # Уровень 2 — язык (компактный детектор): EN vs ES внутри латиницы.
    ...
```

В `provider.py`:

```python
    async def _verify_language(self, candidate, req, fence):
        if candidate is None:
            return None
        v = check_language(candidate.draft_ru, req.generate_language)
        if v.ok:
            return candidate
        # 1 retry с усиленной языковой инструкцией
        self._snapshot["lang_retry"] += 1
        candidate2 = await self._generate_once(req, fence, strict_lang=True)
        if candidate2 is None:
            return candidate            # отдать первый с флагом
        v2 = check_language(candidate2.draft_ru, req.generate_language)
        if v2.ok:
            return candidate2
        # второй промах → lang_ok=False, не крутить дальше
        self._snapshot["lang_failed"] += 1
        return replace(candidate2, lang_ok=False)   # dataclasses.replace
```

> РЕШЕНО: флаг = поле `lang_ok: bool = True` в DraftCandidate (минимум
> правок, дефолт ничего не ломает). UI показывает бейджем «язык под
> вопросом», как `has_gaps`. Записывается в БД (колонка ниже).

---

## 6. Тесты

### 6.1 `tests/test_provider_I2.py` (~14 тестов)

```diff
-def _good_response():
-    return '{"answer": "...", "sources": ["Прайс-лист"], "has_gaps": false, "gap_note": null}'
+def _good_response():
+    return '{"draft_ru": "...", "sources": ["Прайс-лист"], "has_gaps": false, "gap_note": null, "confidence": null, "suggested_clarification": null}'
```

- Все `DraftRequest(...)` вызовы: добавить `generate_language` (или
  полагаться на дефолт `"en"` — но тогда тесты, ждущие русский, поправить).
- +новые тесты: parse `confidence`/`suggested_clarification`; языковая
  проверка (провал→retry→успех; двойной провал→флаг).

### 6.2 `tests/test_draft_guardrails.py`

- +тест: `GuardConfig()` дефолт теперь `strict_numbers=False` — число вне
  библиотеки при `has_gaps_claimed=True` → `ACCEPT_WITH_GAPS`, не REJECT.
- Существующий `test_rejects_unverified_numbers_strict` не трогать (он
  задаёт `strict_numbers=True` явно).
- +тест store с `confidence`/`suggested_clarification` → колонки записаны.

---

## 7. Открытое под-решение: язык-детектор (нужен выбор)

Нарушает lean-deps. Три варианта:

| Вариант | Размер | Точность EN/ES | Трейд-офф |
| :-- | :-- | :-- | :-- |
| `fast-langdetect` | ~1 МБ модель (fasttext) | высокая, 176 языков | одна зависимость + бинарная модель |
| `lingua` (low-accuracy) | ~несколько МБ | очень высокая | тяжелее, больше RAM |
| Встроенный n-gram на языки проекта (ru/en/es/uk/pl) | ~10 КБ кода | средняя, хватает для грубой проверки | ноль зависимостей, ручная поддержка |

**РЕШЕНО: встроенный n-gram** на 5 языков проекта (ru/en/es/uk/pl), ноль
зависимостей. Живёт в `langcheck.py`, профили языков — константами модуля.
Цель грубая (различить язык внутри латиницы), точная классификация не нужна.

---

## Порядок применения (когда дашь go)

1. Миграция БД (+2 колонки) — фундамент, без неё store упадёт.
2. I5 guardrails (поля + мягкий режим).
3. I2 provider (parse + запрос + язык из req).
4. Загрузчик промпта (§4).
5. langcheck (§5) + выбранный детектор (§7).
6. Тесты (§6) — гонять весь набор I2+I5 вместе, число назвать.
7. Зафиксировать отклонение §12↔промпт в CONTRACTS.

Каждый шаг — отдельный коммит, проверяемый изолированно.
