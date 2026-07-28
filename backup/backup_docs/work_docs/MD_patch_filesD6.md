Теперь D4.**Патч `INTERFACES.md`**

Удалить: **от начала строки 428** (`### 4.1 \`app/translation/base.py` ⬜ (D1, middle)`) **до конца строки 504** (закрывающие ``` блока §4.3). Раздел 4.2 попадает в диапазон без изменений — воспроизведён в блоке дословно, чтобы правка была одной.

На место удалённого вставить:

```markdown
### 4.1 `app/translation/base.py` ⬜ (D1, middle)

```python
class TranslationMode(str, Enum): LIVE_LITERAL, LIVE_SAFE, POST_CLEAN

@dataclass(frozen=True, slots=True)
class TranslationRequest:
    text: str; source_language: str; target_language: str
    mode: TranslationMode
    context: tuple[str, ...]         # предыдущие raw_text, окно 4/3/2
    segment_id: str | None

@dataclass(frozen=True, slots=True)
class Change:
    type: Literal["filler_removed", "punctuation", "lost_entity", "other"]
    original: str; replacement: str

@dataclass(frozen=True, slots=True)
class TranslationResult:
    translation_raw: str
    translation_clean: str | None
    changes: tuple[Change, ...]
    provider_request_id: str | None

Auditor = Callable[[TranslationRequest, TranslationResult], TranslationResult]

class TranslationProvider(Protocol):
    name: str
    privacy: PrivacyController
    async def translate(req: TranslationRequest, *,
                        fence: Fence) -> TranslationResult
    async def close() -> None
```

**Обязательства провайдера:**

1. `privacy.require(Capability.TEXT_TO_CLOUD)` до сетевого вызова;
2. `privacy.validate(fence, ...)` после ответа, до возврата результата;
3. ключ берётся из `key_provider` в момент вызова, не хранится в объекте;
4. любая ошибка — подкласс `ProviderError` (наследник `SecretFreeError`);
5. никакого ретрая внутри: повторы — прерогатива `JobQueue`;
6. результат перед возвратом проходит `auditor` (по умолчанию
   `prompts.audit`) — вызов внутри финального `translate`, наследник его
   пропустить не может.

**Аудит инвариантов.** `changes` заполняет не провайдер, а аудит: слова-
паразиты для `LIVE_SAFE` и пропавшие из перевода сущности (числа, суммы,
даты, URL, обвал длины) — `type="lost_entity"`. Аудит исключений не
бросает и перевод не отменяет: подозрение на смысловой дрейф фиксируется
в `edit_log_json`, решение принимает человек.

**Разрыв цикла импортов:** `prompts` импортирует типы из `base`, поэтому
`base` не импортирует `prompts` на уровне модуля — только отложенно
внутри `translate` либо через инжекцию `auditor` фабрикой.

### 4.2 `app/translation/providers/openai_realtime.py` ✅ (D5)

```python
class Transport(Protocol):
    async def send(data: str) -> None
    async def recv() -> str
    async def close() -> None
    def abort() -> None

TransportFactory = Callable[[str, dict[str, str]], Awaitable[Transport]]

class DeltaKind(str, Enum): PARTIAL, COMPLETED

@dataclass(frozen=True, slots=True)
class TranscriptDelta:
    kind: DeltaKind; text: str; utterance_hint: str | None; received_at_ms: int

class OpenAIRealtimeSession:          # реализует TeardownHook
    name = "openai-realtime"
    async def open() -> None          # PrivacyViolation в CONFIDENTIAL
    async def push(pcm: bytes) -> None  # StaleGenerationError после switch
    async def close() -> None
    async def teardown() -> None / def force_close() -> None
    stats: RealtimeStats
```

Инжекция `transport_factory` обязательна для тестируемости: тест J4
перехватывает каждый исходящий байт без сети.

### 4.3 `app/translation/prompts.py` ⬜ (D4, middle)

```python
def build(mode: TranslationMode, req: TranslationRequest) -> tuple[str, str]
    # -> (system_prompt, user_prompt); тексты промптов — из спеки §11 дословно

def format_context(context: tuple[str, ...]) -> str
    # блок «НЕ переводить», общий для всех провайдеров

def validate_response(mode: TranslationMode, raw: str) -> TranslationResult
    # POST_CLEAN обязан вернуть строгий JSON; иначе ProviderResponseInvalid

def filler_changes(req: TranslationRequest) -> tuple[Change, ...]
    # слова-паразиты §11.2, предъявленные к удалению; только LIVE_SAFE

def detect_drift(req: TranslationRequest,
                 translation: str) -> tuple[Change, ...]
    # пропавшие сущности: числа (extract_numbers из I5), URL, обвал длины

def audit(req: TranslationRequest,
          result: TranslationResult) -> TranslationResult
    # filler_changes + detect_drift; вызывается базовым классом D1
```

Проверка имён и названий (`AuditConfig.check_names`) выключена по
умолчанию: транслитерация даёт ложные срабатывания на каждом сегменте.

```
**Что изменилось в контрактах**

| Файл                  | Изменение                                                                                                           | Было → стало                   |
| --------------------- | ------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| `D4_prompts.md`       | +`filler_changes`, `detect_drift`, `audit`, `AuditConfig`; открытый вопрос закрыт вариантом A; +5 критериев приёмки | 200 LOC / 14 тестов → 245 / 19 |
| `D1_provider_base.md` | Вызов аудита в финальном `translate`, `Change.type` +`lost_entity`, разрыв цикла импортов; +3 критерия              | 180 LOC / 8 тестов → 215 / 11  |
| `D2`, `D3`            | Не затронуты — аудит в базовом классе                                                                               | —                              |

**Ограничения выполнения**

1. В выданной вчера версии `D4` была моя ошибка: `validate_response(mode, raw)` не принимает `req`, а п. 8 требовал искать филлеры в `req.text`. Исправлено разделением на `validate_response` (чистый разбор) и `audit(req, result)`.
2. Отложено вашим решением: заводить ли `translation_status='drift_suspect'` — это миграция БД и правка §8 спеки. Пока дрейф виден только в `changes`/`edit_log_json`.
3. Номера строк 428 и 504 — по загруженной в проект версии `INTERFACES.md` (785 строк). Если файл правился после загрузки, ориентируйтесь на текстовые якоря начала и конца.

✓ Патч §4 `INTERFACES.md` выдан одним блоком; `D1` и `D4` обновлены под аудит дрейфа
```
