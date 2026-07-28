# DRAFT-обработчик + интеграция I3 + кэш библиотеки — один блок

Замыкает цепочку черновиков до рабочей: детектор I3 (готов) → постановка
`JobType.DRAFT` в конвейере → обработчик, который гонит I2→guard→store.

Состав:
1. Спека DRAFT-обработчика (в документах её нет — здесь).
2. Патч main.py: регистрация `JobType.DRAFT` + `_handle_draft` + встраивание
   постановки в `_on_stt_result` (фильтр `role='meeting'`).
3. Кэш в `FactLibrary` (I1) — снимает двойное чтение (I2 + guard).
4. Тесты.

Тесты прогоняешь у себя.

---

## 1. Спека DRAFT-обработчика

### Место
Метод `Application._handle_draft(job)` в main.py, регистрируется как
исполнитель `JobType.DRAFT`. Симметричен `_handle_translate`.

### Поток (из готовых I2/I5, не выдумано)
```
_handle_draft(job):
  1. gate: провайдер доступен?  (offline.should_attempt)
  2. собрать DraftRequest из конфига сессии + raw_text сегмента
  3. candidate = await i2.generate(req, fence=job.fence)   # None → тихо выйти
  4. library_text = (await library.get(section_id)).content_text   # кэш
  5. verdict = guard.verify(candidate, library_text)
  6. draft_id = await guard.store(candidate, verdict)      # None если REJECT
  7. draft_id + ui → publish(DRAFT_CREATED, {...})
```

### Правила
1. **fence — из `job.fence`**, не перезахват. I2 сам `require()` не зовёт
   (это провайдер). Тот же инвариант, что TRANSLATE.
2. **`None` от I2 → тихий выход.** Пустой вопрос, битый JSON, провайдер
   вернул пусто — задача завершена без записи, без ретрая.
3. **REJECT от guard → `store` вернёт `None` → тихо.** Черновик с
   выдуманным числом (строгий режим) или «из воздуха» не пишется. Это
   штатная отбраковка, не ошибка задачи.
4. **`StaleGenerationError` → тихо** (профиль сменился между постановкой и
   исполнением; черновик под старым fence не пишем).
5. **`ProviderError` → `offline.mark_unavailable` + `raise`** (queue
   решит ретрай по `exc.retryable`). Симметрично TRANSLATE.
6. **gate BLOCKED → отложить** задачу с задержкой (как TRANSLATE), не
   завершать «успехом» без черновика.
7. **Событие `DRAFT_CREATED`** — только при успешной записи (`draft_id`
   не `None`). Payload — §7.1 + служебные поля (ниже).
8. **Статус задачи в jobs обработчик не трогает** — queue сам.

### Источники полей DraftRequest (решение владельца: из конфига сессии)
| Поле | Источник |
| :-- | :-- |
| `question_text` | `raw_text` сегмента (читается по `segment_id`) |
| `library_section_id` | конфиг сессии (активный раздел из панели) |
| `generate_language` | конфиг сессии (панель: клиент→юзер→en) |
| `tone_preset`, `tone_note` | конфиг сессии (панель) |
| `target_language` | язык потока `meeting` (audio_streams) — для I4 |
| `session_id`, `trigger_segment_id` | из job/сегмента |

> Точка сверки: где Application держит runtime-конфиг сессии
> (`generate_language`, `tone_*`, `library_section_id`). В `sessions` DDL
> есть `library_context_id`, `draft_provider`; язык/тон панели — вероятно
> в объекте сессии в памяти после `start_session`. Сверить имя атрибута.

### Событие DRAFT_CREATED (payload)
§7.1 базовый + служебные поля, которые мы добавили:
```json
{
  "draft_id": "...",
  "trigger_segment_id": "...",
  "draft_ru": "...",
  "sources": [...],
  "has_gaps": false,
  "confidence": null,
  "lang_ok": true
}
```
`confidence`/`lang_ok` — расширение §7.1 под UI-бейджи (согласовано при
добавлении полей). `suggested_clarification` в событие не кладём — оно для
детального вида, не для ленты.

### Запрещено
- Перезахватывать fence.
- Писать черновик мимо `guard.store`.
- Трогать статус задачи в jobs.
- Ставить DRAFT для потока `microphone` (своя речь — не повод для черновика).

---

## 2. Патч main.py

### 2a. Импорты
```python
from app.drafts.provider import DraftProvider, DraftRequest, DraftProviderConfig
from app.drafts.guardrails import DraftGuard
from app.drafts.library import FactLibrary
from app.drafts.trigger import is_question
from app.ui.server import EventType
```

### 2b. Атрибуты `__init__`
```python
        self.library: FactLibrary | None = None
        self.draft_guard: DraftGuard | None = None
        self._draft: DraftProvider | None = None
```

### 2c. Создание в `start()` + регистрация (рядом с TRANSLATE)
```python
        self.library = FactLibrary(self.db)
        self.draft_guard = DraftGuard(self.db)   # strict_numbers из GuardConfig (мягкий)
        self._draft = DraftProvider(self._provider, self.library, DraftProviderConfig())
        self.jobs.register(JobType.DRAFT, self._handle_draft)
```

> `DraftProvider` использует тот же `self._provider` (TranslationProvider),
> что TRANSLATE — I2 гоняет генерацию через `translate` с mode=DRAFT.

### 2d. Обработчик
```python
    async def _handle_draft(self, job) -> None:
        assert self.db and self.offline and self._draft and self.draft_guard and self.library
        segment_id = job.segment_id
        if not segment_id:
            return

        provider = self._provider
        if not self.offline.should_attempt(provider.name):
            await self.jobs.enqueue(
                JobType.DRAFT, segment_id=segment_id,
                idempotency_key=f"dr:{segment_id}", delay_s=30.0,
            )
            return

        # raw_text вопроса + язык встречи (meeting-поток) через JOIN.
        loaded = await self._load_draft_input(segment_id)
        if loaded is None:
            return
        question_text, target_language = loaded

        # Конфиг сессии: раздел, язык генерации, тон.
        cfg = self._session_cfg()          # см. точку сверки
        req = DraftRequest(
            session_id=job.session_id if hasattr(job, "session_id") else self._session_id,
            trigger_segment_id=segment_id,
            question_text=question_text,
            target_language=target_language,
            library_section_id=cfg.library_section_id,
            generate_language=cfg.generate_language,
            tone_preset=cfg.tone_preset,
            tone_note=cfg.tone_note,
        )

        try:
            candidate = await self._draft.generate(req, fence=job.fence)
        except StaleGenerationError:
            return
        except ProviderError as exc:
            self.offline.mark_unavailable(provider.name, exc)
            raise

        self.offline.mark_available(provider.name)
        if candidate is None:
            return                          # генерировать было нечего

        # library_text для verify — из кэша (см. патч I1).
        library_text = (await self.library.get(cfg.library_section_id)).content_text
        verdict = self.draft_guard.verify(candidate, library_text)
        draft_id = await self.draft_guard.store(candidate, verdict)
        if draft_id is None:
            return                          # REJECT — отбраковано, тихо

        if self.ui_server:
            self.ui_server.publish(
                EventType.DRAFT_CREATED,
                {
                    "draft_id": draft_id,
                    "trigger_segment_id": segment_id,
                    "draft_ru": candidate.draft_ru,
                    "sources": list(candidate.sources),
                    "has_gaps": candidate.has_gaps_claimed,
                    "confidence": candidate.confidence,
                    "lang_ok": candidate.lang_ok,
                },
            )
```

### 2e. Загрузка входа (raw_text + язык meeting)
```python
    async def _load_draft_input(
        self, segment_id: str
    ) -> tuple[str, str] | None:
        assert self.db
        row = await self.db.fetch_one(
            "SELECT s.raw_text AS raw_text, st.target_language AS tgt, st.role AS role "
            "FROM segments s JOIN audio_streams st ON st.id = s.stream_id "
            "WHERE s.id = ?",
            (segment_id,),
        )
        if row is None:
            return None
        # Черновик только на реплики собеседника.
        if row["role"] != "meeting":
            return None
        raw_text = row["raw_text"]
        if not raw_text or not raw_text.strip():
            return None
        return (raw_text.strip(), row["tgt"])
```

### 2f. Встраивание постановки в `_on_stt_result`

Рядом с постановкой TRANSLATE, ПОСЛЕ неё. Фильтр `role='meeting'` +
детектор I3. Ставим только на вопросы собеседника.

```python
        # DRAFT: вопрос собеседника (role='meeting') → черновик ответа.
        if role == "meeting":
            is_q, _conf = is_question(raw_text, meeting_language)
            if is_q:
                await self.jobs.enqueue(
                    JobType.DRAFT,
                    segment_id=seg.id,
                    idempotency_key=f"dr:{seg.id}",
                )
```

> Точки сверки в `_on_stt_result`:
> - имя переменной роли (`role`) и языка встречи (`meeting_language`) —
>   если под рукой нет, взять из того же JOIN, что TRANSLATE;
> - постановка идёт только для `track='accurate'` (raw_text есть только там);
> - `is_question` дешёвая и синхронная — вызывать прямо в конвейере ок.

---

## 3. Кэш в `FactLibrary` (I1) — снять двойное чтение

Прозрачный кэш по `context_id` с инвалидацией по `updated_at`. Семантика
`get` не меняется — возвращает тот же `LibraryContext`.

### Патч `app/drafts/library.py`

**3a. Кэш в `__init__`:**
```python
    def __init__(self, db: Database, *, max_tokens: int = 30000) -> None:
        self._db = db
        self._max_tokens = max_tokens
        self._cache: dict[str, LibraryContext] = {}   # НОВОЕ
```

**3b. `get` — проверка версии, чтение при промахе:**

НАЙТИ:
```python
    async def get(self, context_id: str) -> LibraryContext:
        row = await self._db.fetch_one(
            """
            SELECT id, name, domain, content_text, token_estimate, updated_at
            FROM library_contexts
            WHERE id = ?
""",
            (context_id,),
        )
        if not row:
            raise InvariantViolation("library_context_not_found")
        return LibraryContext(
            id=row["id"],
            name=row["name"],
            domain=row["domain"],
            content_text=row["content_text"],
            token_estimate=row["token_estimate"],
            updated_at=row["updated_at"],
        )
```
ЗАМЕНИТЬ НА:
```python
    async def get(self, context_id: str) -> LibraryContext:
        # Лёгкая проверка версии (PK lookup) — ловит перезапись раздела.
        ver = await self._db.fetch_one(
            "SELECT updated_at FROM library_contexts WHERE id = ?",
            (context_id,),
        )
        if not ver:
            self._cache.pop(context_id, None)
            raise InvariantViolation("library_context_not_found")
        cached = self._cache.get(context_id)
        if cached is not None and cached.updated_at == ver["updated_at"]:
            return cached                       # хит: content_text не тащим повторно

        row = await self._db.fetch_one(
            """
            SELECT id, name, domain, content_text, token_estimate, updated_at
            FROM library_contexts
            WHERE id = ?
""",
            (context_id,),
        )
        if not row:
            self._cache.pop(context_id, None)
            raise InvariantViolation("library_context_not_found")
        ctx = LibraryContext(
            id=row["id"],
            name=row["name"],
            domain=row["domain"],
            content_text=row["content_text"],
            token_estimate=row["token_estimate"],
            updated_at=row["updated_at"],
        )
        self._cache[context_id] = ctx
        return ctx
```

**3c. Инвалидация в `upsert`** (после записи, перед `return cid`):
```python
        self._cache.pop(cid, None)              # раздел изменился — сброс кэша
        return cid
```

**3d. Инвалидация в `delete`** (после write):
```python
        self._cache.pop(context_id, None)
```

> ⚠️ Риск для тестов I1: тесты, считающие точное число `fetch_one`, теперь
> увидят +1 вызов (проверка версии). Если такие есть — поправить счётчик
> или мокать по SQL. Семантика возврата не изменилась.

> Честно: экономия скромная (get идёт через reader-пул, не блокирует
> запись; локальный SQLite быстр). Кэш добавлен по требованию для
> предсказуемости «всё под рукой», не ради критичной производительности.

---

## 4. Тесты

### `tests/test_draft_handler.py`
Позитив:
- сегмент meeting с вопросом → fake I2 отдаёт candidate → guard ACCEPT →
  запись в draft_answers, событие DRAFT_CREATED, gate AVAILABLE.

Негативы:
- I2 вернул None → задачи нет записи, тихо;
- guard REJECT (выдуманное число, строгий cfg) → store None → нет записи;
- switch профиля → StaleGenerationError → нет записи;
- ProviderError → mark_unavailable + проброс;
- gate BLOCKED → отложено (delay_s), провайдер не вызван;
- сегмент role='microphone' → `_load_draft_input` вернул None → нет черновика.

### `tests/test_trigger_integration.py` (встраивание)
- meeting-сегмент с «How much?» → задача DRAFT поставлена;
- meeting-сегмент «We agreed.» → задача НЕ поставлена (не вопрос);
- microphone-сегмент с «How much?» → задача НЕ поставлена (своя речь).

### `tests/test_library_I1.py` (кэш)
- два `get` подряд без изменений → второй из кэша (проверить по счётчику
  чтения content_text или подменённому db);
- `upsert` того же раздела → следующий `get` перечитывает (updated_at сменился);
- удалённый раздел → `get` бросает, кэш очищен.

Критерий: полный набор зелёный, число названо.

---

## 5. Порядок применения
1. Кэш I1 (§3) + тесты кэша.
2. DRAFT-обработчик + регистрация (§2a–2e).
3. Встраивание постановки (§2f) — вместе с обработчиком, чтобы задачи не
   копились без исполнителя.
4. Тесты (§4), прогон, число.

## 6. Точки сверки
| Что | Где |
| :-- | :-- |
| runtime-конфиг сессии (`generate_language`, `tone_*`, `library_section_id`) | main.py / объект сессии |
| имя `role`/`meeting_language` в `_on_stt_result` | main.py |
| `JobType.DRAFT` в enum (в схеме jobs.type 'draft' есть) | queue.py |
| `session_id` на job или на Application | queue.py / main.py |
| `EventType.DRAFT_CREATED` значение | server.py (есть в §7.1) |
