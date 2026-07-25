# INTERFACES.md — межмодульные контракты speech-local v2.0

Задача A1 роадмапа. Документ авторитетен: при расхождении кода и этого файла
прав файл, код правится.

**Особенность этой версии.** Сигнатуры не спроектированы умозрительно —
они извлечены из реализованного и прогнанного senior-слоя (20 файлов,
6 126 LOC, 10/10 тестов). Всё, что помечено ✅, существует и работает;
помеченное ⬜ реализуют middle/junior-агенты по контрактам из `CONTRACTS/`.

---

## 0. Общие правила

| Правило | Формулировка |
| :-- | :-- |
| Импорты | Только абсолютные: `from app.audio.pcm import Frame`. Относительные запрещены |
| Асинхронность | Всё, что делает ввод-вывод, — `async`. Блокирующие вызовы уходят в `run_in_executor` |
| Исключения | Только из `app/errors.py`. Свои классы — наследники `SpeechLocalError` |
| Секреты | Классы, пересекающие границу с облаком, наследуют `SecretFreeError`. Ключ в аргументах исключения — блокирующий дефект |
| Датаклассы | Пересекающие границу модулей — `frozen=True, slots=True` |
| Время | Внутри конвейера — миллисекунды от старта потока (`int`). В БД — ISO-8601 UTC (`str`) |
| Наблюдаемость | Каждый долгоживущий компонент имеет `snapshot() -> dict[str, Any]` для экрана E5 |
| Профиль | Компонент **не хранит** копию профиля. Только `PrivacyController` |

---

## 1. Фундамент

### 1.1 `app/errors.py` ✅

```python
SpeechLocalError(Exception)          # code: str, retryable: bool
├── SecretFreeError                  # запрещено класть секреты в args
├── InvariantViolation               # retryable=False всегда
│   ├── ImmutableFieldError(table, field, row_id)
│   └── PrivacyViolation(capability, profile)
├── StaleGenerationError             # штатный исход, не авария
├── StorageError → WriterQueueFull, DatabaseClosed, MigrationError
├── JobError → JobNotFound, LeaseLost, NonRetryableJob
├── AudioError → NodeNotFound, CaptureInterrupted
├── SttError → ModelNotAvailable, SttOutputMalformed
├── ProviderError(SecretFreeError) → ProviderAuthError, ProviderRateLimited,
│                                    ProviderUnavailable, ProviderResponseInvalid
└── DegradationRequired(level, reason)
```

Правило для новых исключений: `retryable=True` ставится **только** если
повтор той же операции может дать иной результат. Нарушение инварианта,
ошибка авторизации и битый ответ — всегда `False`.

### 1.2 `app/db.py` ✅

```python
@dataclass(frozen=True)
class DbConfig:
    path: Path; busy_timeout_ms: int = 5000; writer_queue_max: int = 2000
    reader_threads: int = 4; writer_queue_warn: int = 200
    synchronous: str = "NORMAL"; wal_autocheckpoint: int = 1000

class Database:
    async def start() -> None
    async def close(drain_timeout_s: float = 10.0) -> None
    async def migrate(migrations_dir: Path) -> int      # применено миграций
    async def version() -> int

    async def write(fn: Callable[[sqlite3.Connection], T]) -> T
    async def execute(sql: str, params: Sequence = ()) -> int
    async def execute_many(sql: str, seq_params: Iterable[Sequence]) -> int

    async def fetch_one(sql: str, params: Sequence = ()) -> sqlite3.Row | None
    async def fetch_all(sql: str, params: Sequence = ()) -> list[sqlite3.Row]
    async def read(fn: Callable[[sqlite3.Connection], T]) -> T

    stats: WriterStats                                   # .snapshot()
```

**Контракт `write(fn)`:**
- `fn` выполняется в потоке-писателе внутри одного `BEGIN IMMEDIATE`;
- `fn` **синхронный**, короткий, без сетевого ввода-вывода — он держит
  writer-lock и останавливает запись всего сервиса;
- исключение внутри `fn` откатывает транзакцию целиком;
- несколько операций внутри одного `fn` = одна атомарная транзакция.

**Что запрещено:** прямое создание `sqlite3.connect` где-либо, кроме `db.py`.
Любая запись — через `Database`.

### 1.3 `app/privacy.py` ✅

```python
class PrivacyProfile(str, Enum): OPEN, CONFIDENTIAL
class Capability(str, Enum):
    LOCAL_STT, LOCAL_STORAGE, TEXT_TO_CLOUD, AUDIO_TO_CLOUD,
    DRAFT_GENERATION, LOCAL_EXPORT

CAPABILITY_MATRIX: dict[PrivacyProfile, frozenset[Capability]]
# OPEN: всё. CONFIDENTIAL: всё кроме AUDIO_TO_CLOUD.

@dataclass(frozen=True, slots=True)
class Fence: profile: PrivacyProfile; generation: int

class TeardownHook(Protocol):
    name: str
    async def teardown() -> None      # отменяемый
    def force_close() -> None         # синхронный аварийный

class PrivacyController:
    profile: PrivacyProfile           # property
    generation: int                   # property
    def fence() -> Fence
    def allows(capability) -> bool
    def require(capability) -> Fence           # PrivacyViolation при запрете
    def validate(fence, capability) -> None    # StaleGenerationError / PrivacyViolation
    def is_stale(fence) -> bool
    def register_hook(hook: TeardownHook) -> None
    def unregister_hook(name: str) -> None
    def add_listener(fn: Callable[[PrivacyProfile, int], None]) -> None
    async def switch(target, *, session_id=None, reason="user") -> int  # мс teardown
    def snapshot() -> dict
```

**Протокол использования облачного вызова — обязателен, без исключений:**

```python
fence = privacy.require(Capability.TEXT_TO_CLOUD)   # 1. право + fence атомарно
result = await provider.call(...)                   # 2. сетевой вызов
privacy.validate(fence, Capability.TEXT_TO_CLOUD)   # 3. до записи результата
await db.write(...)                                 # 4. запись
```

Пропуск шага 3 — блокирующий дефект: результат, полученный под старым
поколением, попадёт в БД после переключения профиля.

Декоратор `@guarded(capability)` делает шаги 1 и 3 автоматически; он
применим, только если между захватом fence и отправкой нет собственной
асинхронной логики.

### 1.4 `app/queue.py` ✅

```python
class JobType(str, Enum): STT, TRANSLATE, DRAFT, EXPORT
class JobStatus(str, Enum): QUEUED, RUNNING, DONE, FAILED, CANCELLED
JOB_CAPABILITY: dict[JobType, Capability]

@dataclass(slots=True)
class Job:
    id: str; type: JobType; segment_id: str | None; payload: dict
    status: JobStatus; idempotent: bool; attempts: int; max_attempts: int
    privacy_profile: PrivacyProfile; privacy_gen: int
    lease_owner: str | None; error_code: str | None
    fence: Fence                                   # property

Handler = Callable[[Job], Awaitable[Any]]

class JobQueue:
    async def enqueue(job_type, *, segment_id=None, payload=None,
                      idempotent=True, idempotency_key=None,
                      max_attempts=None, delay_s=0.0) -> str
    def register(job_type: JobType, handler: Handler) -> None
    async def start() -> None                      # recover() внутри
    async def stop(timeout_s: float = 10.0) -> None
    async def recover() -> dict[str, int]          # {"requeued": n, "failed": m}
    async def retry_manually(job_id: str) -> None
    async def cancel_by_fence(capability: Capability) -> int
    def pause(job_type) -> None / def resume(job_type) -> None
    async def stats() -> dict
```

**Контракт обработчика (`Handler`):**
- успех = возврат без исключения; результат не используется очередью;
- провал = исключение; ретрай определяется `exc.retryable` **и**
  `job.idempotent`, а не решением обработчика;
- обработчик обязан быть идемпотентным, если задача поставлена с
  `idempotent=True` (значение по умолчанию);
- обработчик **не** трогает статус задачи в БД сам.

---

## 2. Аудиотракт

### 2.1 `app/audio/pcm.py` ✅

```python
SAMPLE_RATE = 16000; CHANNELS = 1; SAMPLE_WIDTH = 2; FRAME_MS = 20

@dataclass(frozen=True, slots=True)
class Frame:
    pcm: bytes; t_start_ms: int
    t_end_ms: int      # property
    rms: float         # property, 0..1
    dbfs: float        # property

def bytes_to_ms(n) -> int / ms_to_bytes(ms) -> int
def rms_s16(pcm) -> float / dbfs_s16(pcm) -> float / peak_s16(pcm) -> float
def write_wav(path: Path, pcm: bytes) -> None    # блокирующая
def read_wav(path: Path) -> bytes

class FrameSplitter:
    def push(chunk: bytes) -> list[Frame]
    def flush() -> Frame | None
    def reset(offset_ms: int = 0) -> None
    position_ms: int    # property
```

Формат зафиксирован жёстко: 16 кГц / моно / s16le. Любой код, принимающий
`bytes` как аудио, считает их в этом формате без проверки.

### 2.2 `app/audio/discovery.py` ✅

```python
class NodeKind(str, Enum):
    MICROPHONE, SINK_MONITOR, APPLICATION_OUTPUT, VIRTUAL_SOURCE, OTHER

@dataclass(frozen=True, slots=True)
class AudioNode:
    node_id: int; name: str; description: str; media_class: str
    kind: NodeKind; channels: int | None; sample_rate: int | None
    app_name: str | None; is_default: bool
    display: str        # property
    def to_config() -> str          # сохраняется node.name, НЕ id

class PipeWireDiscovery:
    def available() -> bool
    async def diagnose() -> dict
    async def refresh() -> list[AudioNode]
    def all() / capturable() -> list[AudioNode]
    def by_kind(kind) -> list[AudioNode]
    def resolve(node_name: str) -> AudioNode                 # NodeNotFound
    async def resolve_fresh(node_name: str) -> AudioNode
```

**Правило идентификации:** в конфиге и БД хранится `node.name`. Числовой
`node_id` нестабилен между переподключениями устройства и не сохраняется.

### 2.3 `app/audio/capture.py` ✅

```python
StreamRole = Literal["microphone", "meeting"]

@dataclass(frozen=True, slots=True)
class AudioChunk:
    role: StreamRole; pcm: bytes; t_start_ms: int; duration_ms: int; rms: float
    t_end_ms: int   # property

@dataclass(frozen=True, slots=True)
class GapEvent:
    role: StreamRole; at_ms: int; duration_ms: int; reason: str

class CaptureStream:
    def start() -> None / def stop(timeout_s=3.0) -> None     # async
    def __aiter__() -> AsyncIterator[AudioChunk | GapEvent]
    def snapshot() -> dict

class CaptureManager:
    def add(config: CaptureConfig) -> CaptureStream
    async def start_all() / stop_all() -> None
    def snapshot() -> dict
```

**Контракт потока:** итератор отдаёт `AudioChunk` подряд; `GapEvent`
означает разрыв непрерывности — потребитель обязан закрыть текущий буфер и
не склеивать аудио по обе стороны разрыва.

**⚠️ Отклонение от спеки §7, ожидает утверждения владельца:** нормализация
делается самим `pw-record` (16 кГц/моно/s16le), FFmpeg — резервный бэкенд,
не в горячем пути. Экономия: один процесс и 40–80 МБ RAM на два потока.

### 2.4 `app/audio/vad.py` ✅

```python
class VadState(str, Enum): SILENCE, RISING, SPEECH, FALLING
class VadEventType(str, Enum): SPEECH_START, SPEECH_END

@dataclass(frozen=True, slots=True)
class VadEvent:
    type: VadEventType; at_ms: int; noise_floor_db: float; level_db: float

@dataclass(frozen=True, slots=True)
class VadConfig:
    onset_offset_db=9.0; release_offset_db=5.0; absolute_floor_db=-55.0
    onset_debounce_ms=100; hangover_ms=220
    noise_rise_per_s_db=3.0; noise_fall_per_s_db=24.0; calibration_ms=600

class VadDetector:
    def process(frame: Frame) -> list[VadEvent]
    state: VadState; in_speech: bool; noise_floor_db: float
    last_active_ms: int | None
    def silence_duration_ms(now_ms: int) -> int
    def reset(*, recalibrate: bool = False) -> None
    stats: VadStats
```

Замена реализации (например, на Silero в тире 2) обязана сохранить этот
интерфейс — сегментатор зависит только от него.

### 2.5 `app/audio/segmenter.py` ✅

```python
class CloseReason(str, Enum): PAUSE, MAX_LENGTH, GAP, SHUTDOWN

@dataclass(frozen=True, slots=True)
class SegmentConfig:
    role: StreamRole; session_dir: Path
    silence_close_ms: int = 800        # дефолт по бюджету задержки
    min_segment_ms: int = 800; max_segment_ms: int = 15000
    cut_search_ms: int = 700; preroll_ms: int = 300; tail_keep_ms: int = 200
    partial_interval_ms: int = 600; partial_min_ms: int = 400
    hard_buffer_ms: int = 30000

@dataclass(frozen=True, slots=True)
class FinalSegment:      # ТОЧНЫЙ трек
    id: str; role: StreamRole; t_start_ms: int; t_end_ms: int
    audio_path: Path; reason: CloseReason; mean_level_db: float
    duration_ms: int     # property

@dataclass(frozen=True, slots=True)
class PartialUtterance:  # БЫСТРЫЙ трек, в raw_text не пишется НИКОГДА
    utterance_id: str; role: StreamRole; t_start_ms: int; t_end_ms: int
    pcm: bytes; sequence: int

class Segmenter:
    async def run(source) -> AsyncIterator[FinalSegment | PartialUtterance]
    async def flush(reason=CloseReason.SHUTDOWN) -> list[SegmenterEvent]
    def snapshot() -> dict
```

---

## 3. STT

### 3.1 `app/stt/runner.py` ✅

```python
@dataclass(frozen=True, slots=True)
class WhisperConfig:
    binary: Path; model_path: Path; fallback_model_path: Path
    threads: int = 4; language: str = "auto"
    timeout_factor: float = 2.0; timeout_base_s: float = 10.0
    beam_size: int = 0; extra_args: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class WhisperRawResult:
    json_path: Path; payload: dict; model_used: str
    wall_ms: int; audio_ms: int
    realtime_factor: float      # property

class WhisperRunner:
    async def transcribe(wav_path, audio_ms, *, model_path=None,
                         language=None) -> WhisperRawResult
```

`payload` — сырой JSON whisper. **Разбор смысла делает только
`app/stt/parser.py` (C6)**; остальные модули к `payload` не обращаются.

### 3.2 `app/stt/scheduler.py` ✅

```python
ResultSink = Callable[[FinalSegment, WhisperRawResult], Awaitable[None]]
ErrorSink  = Callable[[FinalSegment, BaseException], Awaitable[None]]

class SttScheduler:
    def submit(segment: FinalSegment) -> bool     # False = очередь переполнена
    async def start() / stop(timeout_s=30.0) -> None
    backlog_ms: int; backlogged: bool             # property
    def snapshot() -> dict
```

**Инвариант:** ровно один вызов whisper одновременно, оба потока идут через
эту очередь. `submit() -> False` не означает потерю: WAV на диске, вызывающий
обязан поставить задачу `JobType.STT` с задержкой.

Приоритет: `microphone` (0) выше `meeting` (1); внутри — FIFO по `t_start_ms`.

### 3.3 `app/stt/fallback.py` ✅

```python
class ModelTier(str, Enum): BASE, TINY

class ModelSelector:
    tier: ModelTier; current_path: Path            # property
    def observe(realtime_factor: float) -> ModelTier
    def force(tier: ModelTier, reason: str) -> None
    def snapshot() -> dict
```

### 3.4 `app/stt/parser.py` ⬜ (C6, middle)

```python
@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    detected_language: str | None
    confidence: float | None       # усреднённый avg_logprob, None если нет
    words: tuple[Word, ...]        # пустой кортеж допустим

@dataclass(frozen=True, slots=True)
class Word:
    text: str; t_start_ms: int; t_end_ms: int; probability: float | None

def parse(raw: WhisperRawResult) -> Transcript     # SttOutputMalformed
```

Контракт: `CONTRACTS/C6_whisper_parser.md`.

### 3.5 `app/stt/language.py` ⬜ (C8, middle)

```python
@dataclass(frozen=True, slots=True)
class LanguageDecision:
    effective: str          # что уходит в перевод
    configured: str
    detected: str | None
    conflict: bool          # True -> индикатор в UI
    note: str | None

def resolve(configured: str, detected: str | None,
            autodetect_enabled: bool) -> LanguageDecision
```

**Правило спеки §4:** при конфликте для перевода используется **заданный**
язык; детектированный фиксируется и показывается индикатором. Автопереход
запрещён.

---

## 4. Перевод

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

### 4.4 `app/translation/context.py` ⬜ (D6, middle)

```python
@dataclass(frozen=True, slots=True)
class ContextConfig:
    window_short: int = 4; window_mid: int = 3; window_long: int = 2
    short_chars: int = 100; long_chars: int = 200

async def build_context(db: Database, segment_id: str,
                        cfg: ContextConfig) -> tuple[str, ...]
```

Правило: размер окна выбирается по длине **текущего** сегмента. Берутся
только `track='accurate'` того же `stream_id`, строго предшествующие по
`t_start_ms`.

### 4.5 `app/translation/supersede.py` ✅ (D8)

```python
@dataclass(frozen=True, slots=True)
class SupersedeResult:
    accurate_id: str; superseded_fast_ids: tuple[str, ...]; count: int

class SupersedeService:
    async def link(accurate_segment_id: str) -> SupersedeResult   # идемпотентно
    async def expire_orphans(now_ms: int, session_id: str) -> int
    async def export_view(session_id: str) -> list[sqlite3.Row]   # только accurate
    async def stats(session_id: str) -> dict
```

### 4.6 `app/translation/offline.py` ⬜ (D7, middle)

```python
class OfflineGate:
    def mark_unavailable(provider: str, err: ProviderError) -> None
    def mark_available(provider: str) -> None
    def is_degraded() -> bool
    async def catch_up(db: Database, queue: JobQueue) -> int   # догон
    def snapshot() -> dict
```

Инвариант: недоступность облака **не** останавливает локальный STT и запись.

---

## 5. Черновики ответов

### 5.1 `app/drafts/guardrails.py` ✅ (I5)

```python
class VerdictKind(str, Enum): ACCEPT, ACCEPT_WITH_GAPS, REJECT

@dataclass(frozen=True, slots=True)
class DraftCandidate:
    session_id: str; trigger_segment_id: str; draft_ru: str
    target_language: str; sources: tuple[str, ...]
    has_gaps_claimed: bool; gap_note: str | None

@dataclass(frozen=True, slots=True)
class Verdict:
    kind: VerdictKind; unverified_numbers: tuple[float, ...]
    reasons: tuple[str, ...]; accepted: bool   # property

def extract_numbers(text: str) -> set[float]   # «30 000» == «30000€» == «30к»

class DraftGuard:
    def verify(candidate: DraftCandidate, library_text: str) -> Verdict
    async def store(candidate, verdict) -> str | None   # None = отклонён
    async def attach_translation(draft_id: str, translated: str) -> None
    async def mark(draft_id: str, status: Literal["ignored","copied"]) -> None
    async def stats(session_id: str) -> dict
```

**Единственный путь записи черновика — `DraftGuard.store`.** Прямые INSERT
в `draft_answers` из любого другого модуля запрещены; `'copied'` ставит
только `app/delivery` по факту действия пользователя.

### 5.2 `app/drafts/library.py` ⬜ (I1, middle)

```python
@dataclass(frozen=True, slots=True)
class LibraryContext:
    id: str; name: str; domain: str | None
    content_text: str; token_estimate: int; updated_at: str

class FactLibrary:
    async def list() -> list[LibraryContext]
    async def get(context_id: str) -> LibraryContext
    async def upsert(name, domain, content_text) -> str
    def estimate_tokens(text: str) -> int
    def check_limit(text: str, max_tokens: int = 30000) -> tuple[bool, int]
```

Превышение 30k токенов — не молчаливое усечение, а явная ошибка с указанием
фактического размера (спека §12: больше 150k — переход на RAG, тир 2).

### 5.3 `app/drafts/provider.py` ⬜ (I2, middle+)

```python
@dataclass(frozen=True, slots=True)
class DraftResult:
    draft_ru: str; sources: tuple[str, ...]
    has_gaps: bool; gap_note: str | None

class DraftProvider(Protocol):
    async def generate(question: str, library: LibraryContext,
                       dialog_history: tuple[str, ...], *,
                       fence: Fence) -> DraftResult
```

Промпт — из спеки §12 дословно. Prompt caching обязателен: библиотека
вставляется префиксом, неизменным в пределах сессии.

### 5.4 `app/drafts/trigger.py` ⬜ (I3, middle)

```python
def is_question(text: str, language: str) -> tuple[bool, float]
    # -> (решение, уверенность 0..1)
```

Ложноположительное срабатывание дешевле пропуска: лишний черновик
пользователь игнорирует, пропущенный вопрос теряет функцию.

---

## 6. Устойчивость и безопасность

### 6.1 `app/watchdog/degradation.py` ✅ (F2)

```python
class Level(IntEnum): NORMAL=0, LATENCY=1, MEMORY_SOFT=2, MEMORY_HARD=3

class Actions(Protocol):              # реализует app/main.py
    async def shorten_segments(enable: bool) -> None
    async def pause_cloud_jobs(pause: bool) -> None
    async def drop_caches() -> None
    async def stop_stt() -> None
    async def resume_stt() -> None
    async def force_tiny_model(enable: bool) -> None

class MemoryReader:
    source: str                        # "cgroup" | "vmrss"
    def current_mb() -> float

class DegradationCascade:
    def __init__(actions, config=None, *, memory_reader=None,
                 latency_source: Callable[[], list[int]] = ...,
                 backlog_source: Callable[[], bool] = ...)
    async def start() / stop() -> None
    async def tick() -> Level          # публичен ради тестируемости
    level: Level; def snapshot() -> dict
```

### 6.2 `app/watchdog/memory.py` ⬜ (F1, middle+)

Поставщик метрик для каскада: периодический опрос, история для E5,
предупреждение при `source == "vmrss"` (дочерний whisper не учитывается).

### 6.3 `app/security/redactor.py` ⬜ (G1, middle)

```python
class LogRedactor(logging.Filter):
    def add_pattern(regex: str) -> None
    def redact(text: str) -> str
    def filter(record: logging.LogRecord) -> bool   # правит msg, args И exc_text
```

Подключается ко **всем** логгерам в `app/logging_setup.py` до первого
логирующего вызова. Трейсы исключений обрабатываются наравне с сообщениями.

### 6.4 `app/security/byok.py` ⬜ (G2, middle+)

```python
class KeyStore:                        # только RAM, TTL 60 мин
    def put(provider: str, key: str) -> None
    def get(provider: str) -> str      # ProviderAuthError если нет/истёк
    def revoke(provider: str | None = None) -> None
    def masked(provider: str) -> str   # "sk-…XYZ" для UI
    def snapshot() -> dict             # без ключей, только TTL и наличие
```

`get` — это и есть `key_provider` в интерфейсах провайдеров. Запись ключа
куда-либо, кроме памяти этого объекта, — блокирующий дефект.

---

## 7. UI и экспорт

### 7.1 `app/ui/server.py` ⬜ (E1, middle)

События SSE (`event:` / `data:` JSON):

| Событие | Payload |
| :-- | :-- |
| `segment.partial` | `{utterance_id, role, text, t_start_ms, track:"fast"}` |
| `segment.final` | `{segment_id, role, raw_text, t_start_ms, t_end_ms, track:"accurate"}` |
| `segment.translated` | `{segment_id, translation, mode, superseded_ids[]}` |
| `draft.created` | `{draft_id, trigger_segment_id, draft_ru, sources[], has_gaps}` |
| `draft.translated` | `{draft_id, draft_translated}` |
| `privacy.changed` | `{profile, generation, teardown_ms}` |
| `status` | `{state, degradation_level, backlog_ms, latency_ms}` |

**Контракт клиента (E2):** дедупликация по `(тип, id, sequence)`; при
reconnect запрашивается снапшот состояния, а не переигрывание истории.

### 7.2 `app/exports/` ⬜ (G4, G5, junior)

```python
def to_txt(rows: list[sqlite3.Row]) -> str
def to_json(session: dict, rows: list, drafts: list) -> str
def to_srt(rows: list[sqlite3.Row]) -> str
def to_vtt(rows: list[sqlite3.Row]) -> str
```

Вход всегда — результат `SupersedeService.export_view()`. Результаты
быстрого трека в экспорт не попадают (критерий приёмки 13).

### 7.3 `app/delivery/clipboard.py` ⬜ (G3, junior+)

```python
async def copy(text: str) -> bool          # xclip/wl-copy
async def copy_draft(guard: DraftGuard, draft_id: str, text: str) -> bool
    # при успехе -> guard.mark(draft_id, "copied")
```

Единственный путь наружу процесса. Автовставка в чат — тир 2.

---

## 8. Сборка

### `app/main.py` ✅

```python
class Application:
    async def start() -> None
    async def start_session(meeting_title: str | None = None) -> str
    async def stop_session() -> None
    async def shutdown() -> None
    def snapshot() -> dict
```

**Порядок запуска:** БД → профили → очередь задач → STT → (сессия) захват →
сегментация → UI.

**Порядок остановки — строго обратный:** intake → сегментаторы (flush
хвоста) → STT (дорабатывает очередь) → облачные сессии (teardown) →
очередь задач → БД (дренаж писателя + checkpoint WAL).

Нарушение порядка теряет данные: остановка БД раньше STT теряет результаты
распознавания; остановка STT раньше сегментаторов теряет последнюю реплику.

---

## 9. Реестр статусов

| Модуль | Статус | Задача | Уровень |
| :-- | :-- | :-- | :-- |
| `errors`, `db`, `queue`, `privacy` | ✅ | B4, B5, J1 | Senior |
| `audio/pcm`, `discovery`, `capture`, `vad`, `segmenter` | ✅ | C1, C2, C5 | Senior |
| `stt/runner`, `scheduler`, `fallback` | ✅ | C4, C7 | Senior |
| `translation/supersede`, `providers/openai_realtime` | ✅ | D8, D5 | Senior |
| `drafts/guardrails`, `watchdog/degradation` | ✅ | I5, F2 | Senior |
| `main` | ✅ | F3 | Senior |
| `stt/parser`, `language` | ⬜ | C6, C8 | Middle |
| `translation/base`, `prompts`, `context`, `offline` | ⬜ | D1, D4, D6, D7 | Middle |
| `providers/gemini_text`, `claude_text`, `custom_http` | ⬜ | D2, D3 | Middle |
| `drafts/library`, `provider`, `trigger`, `translate` | ⬜ | I1–I4 | Middle |
| `security/redactor`, `byok` | ⬜ | G1, G2 | Middle |
| `watchdog/memory`, `logging_setup`, `ui/server` | ⬜ | F1, E1 | Middle |
| `config`, `models`, `exports/*`, `delivery/*`, UI-статика | ⬜ | B1, B3, E3–E7, G3–G5 | Junior |

---

## 10. Открытые решения владельца

| # | Вопрос | Блокирует |
| :-- | :-- | :-- |
| 1 | Утвердить отклонение §7: нормализация в `pw-record`, FFmpeg — резерв | Возврат `audio/normalizer.py` в план |
| 2 | Строгий режим `DraftGuard` (`strict_numbers=True`) как дефолт | I2, I5 |
