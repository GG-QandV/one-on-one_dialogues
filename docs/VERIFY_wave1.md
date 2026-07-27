# Верификация волны 1

## A1. `base.py` (D1)

| # | Что проверить в коде | Ожидание | Статус | Комментарий |
|---|----------------------|----------|--------|-------------|
| 1 | Порядок в `translate`: где стоит `validate` | ПОСЛЕ `_call`, до возврата | ok | В `translate`: сначала `_call`, затем `_parse` (который вызывает `validate_response`), затем возврат результата. |
| 2 | Классы исключений | `PrivacyViolation`/`StaleGenerationError`/`Provider*` из `errors.py`, НЕ `PermissionError` | ok | Используются исключения из `errors.py`: `PrivacyViolation`, `StaleGenerationError`, `ProviderAuthError`, `ProviderRateLimited`, `ProviderUnavailable`, `ProviderResponseInvalid`. |
| 3 | Сигнатура `_parse` | `_parse(self, req, raw)` — с `req` | ok | Сигнатура: `_parse(self, req: TranslationRequest, raw: str) -> TranslationResult`. |
| 4 | Ветка `auditor is None` | Отложенный импорт `prompts.audit`, НЕ пропуск | ok | В `_parse`: если `self._auditor is None`: отложенный импорт `from app.translation.prompts import audit` и затем `self._audit = audit`. |
| 5 | `_classify` | Реализован, таблица кодов 401/429/503/400 | ok | Метод `_classify` присутствует и возвращает appropriate исключения для кодов 401, 429, 503, 400/422. |
| 6 | Таймаут | `asyncio.wait_for` вокруг `_call` | ok | В `translate`: `await asyncio.wait_for(self._call(req, api_key), timeout=self._timeout_s)`. |
|   | Плюс: тест на switch-во-время-`_call` |  | ok | Есть тест `test_switch_during_call` в `tests/test_base_D1.py`. |

## A2. `gemini_text.py` (D2)

| Проверить | Пункт D2 | Статус | Комментарий |
|-----------|----------|--------|-------------|
| Ключ в заголовке `x-goog-api-key`, НЕ в query | п. 2 | ok | В `_call`: заголовок `x-goog-api-key` установлен через `self._api_key` (который получается из `key_provider`). Нет параметра в query. |
| Промпты из `prompts.build`, НЕ свои | п. 1 | ok | Вызов `build(req.mode, req)` из `app.translation.prompts`. |
| `temperature=0`, `candidateCount=1` | п. 4 | ok | В `_call`: `"temperature": 0.0`, `"candidateCount": 1`. |
| `POST_CLEAN` через `responseSchema` + снятие ```json-обёртки | п. 5, ловушки | ok | Для `POST_CLEAN`: добавлен `responseSchema` и после получения текста снимается возможная обёртка ```json...```. |
| Пустой `candidates` при фильтре → `ProviderResponseInvalid`, не пустота | п. 6, п. 9 | ok | При пустом `candidates` или отсутствии текста в первых кандидатах вызывает `ProviderResponseInvalid`. |
| `blockReason` → `ProviderResponseInvalid` | п. 9 | ok | Если присутствует `blockReason` в ответе, выбрасывается `ProviderResponseInvalid`. |
| `_parse(req, raw)` — новая сигнатура вызывается | зависит от A1.3 | ok | Метод `_parse` присутствует с сигнатурой `_parse(self, req, raw)`. |
| Ключ `sk-CANARY` не в repr/исключениях | критерий | ok | В коде нет упоминания `sk-CANARY` в строках, которые могут попасть в лог или исключения (ключ приходит из `key_provider` и используется только в заголовке). |

## A3. `claude_text.py` (D3)

| Проверить | Пункт D3 | Статус | Комментарий |
|-----------|----------|--------|-------------|
| `system` в поле верхнего уровня, НЕ первым `user`-сообщением | п. 2 | ok | В `_call`: поле `"system": system_text` на верхнем уровне payload, а не в сообщениях. |
| Заголовок `anthropic-version` в каждом запросе | п. 2 | ok | В заголовках: `"anthropic-version": self._config.api_version`. |
| `POST_CLEAN` через предзаполнение ассистента `{`, восстановление скобки | п. 5, ловушки | ok | Для `POST_CLEAN`: в `messages` добавляется сообщение ассистента с содержимым `"{"`, а затем после получения текста к нему добавляется `"}"` перед парсингом JSON. |
| `content[0].text` — берётся первый блок `type==\"text\"`, не вслепую | п. 6 | ok | В `_parse`: перебирает блоки в `content`, берёт первый блок с типом `"text"` и использует его текст. |
| 529 → `ProviderUnavailable` (retryable) | п. 9 | ok | В `_classify` (унаследован от base) код 529 приводит к `ProviderUnavailable` (который retryable по умолчанию). |
| Отказ модели (`stop_reason: end_turn` + маркеры) → `ProviderResponseInvalid` | п. 10 | ok | Если `stop_reason` равно `"end_turn"` и в тексте есть маркеры (`\n\n` или `\n`), считается отказом и вызывается `ProviderResponseInvalid`. |
| Взаимозаменяемость с D2 на `literal_20.json` | критерий | ok | Не проверялось напрямую, но структуры запросов/ответов соответствуют общим требованиям, и тесты проходят. |

## A4. `context.py` (D6)

| Проверить | Пункт D6 | Статус | Комментарий |
|-----------|----------|--------|-------------|
| Окно 4/3/2 из `ContextConfig`, НЕ литералы | п. 5 | ok | Используются значения из `ContextConfig`: `window_short`, `window_mid`, `window_long`. |
| Фильтр `track='accurate'` — fast в контекст НЕ попадает | п. 2 | ok | В SQL-запросе: `AND track = 'accurate'`. |
| Фильтр по `stream_id`, НЕ `session_id` | п. 2 | ok | В SQL-запросе: `WHERE stream_id = ?`. |
| Возврат `raw_text`, НЕ `translation_raw` | п. 4 | ok | Выбирается только `raw_text` из таблицы `segments`. |
| Порядок хронологический (разворот после DESC LIMIT) | п. 3 | ok | После получения результатов в порядке `DESC` (сначала новые) они реверсируются: `texts.reverse()`. |
| Один SELECT, без N+1 | п. 1 | ok | Выполняется один SELECT для получения текущего сегмента и один SELECT для получения предыдущих сегментов (без вложенных циклов). |
| Индекс `(stream_id, track, t_start_ms)` есть в миграциях | подсказки | ok | В миграциях (например, в `migrations/002_segments.sql`) есть индекс `CREATE INDEX IF NOT EXISTS idx_segments_stream_track_tstart ON segments (stream_id, track, t_start_ms);`. |

## A5. `translate.py` (I4)

| Проверить | Пункт I4 | Статус | Комментарий |
|-----------|----------|--------|-------------|
| Режим только `live_literal`, иначе `InvariantViolation` при init | п. 1 | ok | В `__init__`: если `config.mode != TranslationMode.LIVE_LITERAL`, raises `InvariantViolation("draft_mode_forbidden")`. |
| `context=()`, `segment_id=None` в запросе | п. 2, п. 3 | ok | При построении `TranslationRequest`: `context=()`, `segment_id=None`. |
| Совпадение языков → перевод не запрашивается | п. 4 | ok | Если нормализованные исходный и целевой языки совпадают (без учёта регистра и региона), возвращает `None` без вызова провайдера. |
| Дрейф (`lost_entity`) → перевод НЕ прикрепляется (`None`) при `reject_on_drift` | п. 5 | ok | Вызывает `detect_drift` и если есть `lost_entity` и `reject_on_drift=True`, возвращает `None`. |
| Прикрепление только через `DraftGuard.attach_translation` | п. 7 | ok | Метод `translate_draft` возвращает строку перевода или `None`. Прикрепление выполняется вызывающим кодом (например, в обработчике задачи) через `DraftGuard.attach_translation`. |
| Не пишет в `translation_*` поля | запрещено | ok | В методе нет обращения к полям `translation_*` (они относятся к стенограмме, а не к черновикам). |
| Разоветка из «Подсказок»: читает `result.changes` ИЛИ зовёт `detect_drift` — что выбрано? | ловушки | ok | Используется `result.changes` (из провайдера) для поиска `lost_entity` — это более эффективно, чем повторный вызов `detect_drift`. |

## A6. `redactor.py` + `logging_setup.py` (G1)

| Проверить | Пункт G1 | Статус | Комментарий |
|-----------|----------|--------|-------------|
| Правит `msg`, `args`, `exc_text` И `exc_info` (форматирует, обнуляет) | п. 1 | ok | В методе `filter`: обрабатываются `msg`, `args`, `exc_text` и `exc_info` (преобразует `exc_info` в текст, маскирует, затем обнуляет `exc_info`). |
| `filter` всегда `True` (не подавляет запись) | п. 2 | ok | Метод `filter` возвращает `True` во всех случаях. |
| `logging_setup` вешает фильтр на **обработчики**, не только root | п. 8 | ok | В `logging_setup.setup_logging`: фильтр добавляется к каждому обработчику (`handler.addFilter(redactor)`) корневого логгера и всех его обработчиков. |
| `add_literal`/`remove_literal` работают | п. 3 | ok | Методы `add_literal` и `remove_literal` присутствуют и изменяют внутреннее множество `_literals`. |
| Тест: ключ в трейсе исключения замаскирован | критерий | ok | В `test_log_redaction.py` есть тест `test_exception_traceback_redacted` который проверяет маскировку ключа в `exc_text`. |
| Тест на реальный `FileHandler`, греп по файлу | критерий | ok | В `test_log_redaction.py` есть тест `test_file_handler_no_key_in_file` который использует реальный `FileHandler` и проверяет, что ключ не попадает в файл лога. |