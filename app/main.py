"""app/main.py — точка входа и жизненный цикл. Задача F3 + сборка среза.

Спека: раздел 17 «Graceful shutdown: стоп intake → завершение текущего
сегмента → закрытие облачных сессий → сброс SQLite WAL → сохранение
состояния очереди».

Что здесь и чего здесь нет
--------------------------
Здесь — композиция и порядок: создание компонентов, склейка конвейера,
последовательность запуска и остановки. Логики предметной области нет:
каждый компонент самодостаточен и тестируется без main.

Порядок остановки — зеркало запуска
------------------------------------
Запуск:   БД → очередь jobs → STT scheduler → захват → сегментация → UI
Остановка: UI → intake (захват) → сегментаторы (flush хвоста) → STT (дорабатывает
очередь) → облачные сессии (teardown через PrivacyController) → очередь jobs
→ БД (дренаж писателя + checkpoint WAL).

Нарушение порядка теряет данные: остановить БД раньше STT — потерять
результаты распознавания; остановить STT раньше сегментаторов — потерять
хвостовую реплику.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.audio.capture import CaptureConfig, CaptureManager
from app.audio.discovery import PipeWireDiscovery
from app.audio.segmenter import (
    FinalSegment,
    PartialUtterance,
    SegmentConfig,
    Segmenter,
)
from app.config import SttSection, default_stt_section
from app.db import Database, DbConfig
from app.errors import ProviderError, SpeechLocalError, StaleGenerationError
from app.privacy import PrivacyController, PrivacyProfile
from app.queue import JobQueue, JobType, QueueConfig
from app.security.byok import KeyStore
from app.security.keyfiles import load_key_file
from app.stt.base import SttResult
from app.stt.chain import SttChainExhausted
from app.stt.factory import build_stt_chain
from app.stt.scheduler import SchedulerConfig, SttScheduler
from app.translation.base import TranslationMode, TranslationProvider, TranslationRequest
from app.translation.context import ContextConfig
from app.translation.offline import OfflineConfig, OfflineGate
from app.translation.providers.claude_text import ClaudeConfig, ClaudeTextProvider
from app.translation.providers.gemini_text import GeminiTextProvider
from app.translation.supersede import SupersedeService
from app.ui.server import EventType, UiConfig, UiServer

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class TranslationProviderSection:
    active: str = "gemini"
    endpoint: str = ""
    model: str = ""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    translation: TranslationProviderSection = field(default_factory=TranslationProviderSection)


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Собранная конфигурация. Наполняется из config.toml модулем config.py
    (задача B1, middle); здесь — только структура и дефолты для сборки."""
    data_dir: Path = Path("data")
    scheduler: SchedulerConfig = SchedulerConfig()
    queue: QueueConfig = QueueConfig()
    default_profile: PrivacyProfile = PrivacyProfile.OPEN
    streams: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    ui: UiConfig = UiConfig()  # UI server configuration
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    #: Секция [stt] из config.toml. None = дефолты (тесты, запуск без файла).
    stt: SttSection | None = None
    #: Путь к config.toml для POST /api/stt (запись из дашборда).
    config_path: Path = Path("config.toml")
    #: Каталог локальных секретов для STT-ключей (/<key_name>), не TOML.
    secrets_dir: Path = Path.home() / ".secrets"

    def stream_settings(self, role: str) -> dict[str, Any]:
        defaults = {
            "microphone": {"source_language": "ru", "target_language": "en",
                           "node": "", "enabled": True},
            "meeting": {"source_language": "en", "target_language": "ru",
                        "node": "", "enabled": True},
        }
        merged = dict(defaults.get(role, {}))
        if self.streams and role in self.streams:
            merged.update(self.streams[role])
        return merged


class Application:
    """Владелец жизненного цикла. Один экземпляр на процесс."""

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config
        self._stopping = asyncio.Event()

        # Компоненты создаются в start(): порядок создания фиксирован,
        # частично собранное состояние наружу не отдаётся.
        self.db: Database | None = None
        self.privacy: PrivacyController | None = None
        self.jobs: JobQueue | None = None
        self.stt: SttScheduler | None = None
        self.supersede: SupersedeService | None = None
        self.capture: CaptureManager | None = None
        self._segmenters: dict[str, Segmenter] = {}
        self._pipelines: list[asyncio.Task[None]] = []
        self._session_id: str | None = None
        self.ui_server: UiServer | None = None
        self.keystore: KeyStore | None = None
        self.offline: OfflineGate | None = None
        self._provider: TranslationProvider | None = None
        self._draft_provider: Any = None
        self._draft_guard: Any = None
        self._library: Any = None
        self._draft_translator: Any = None
        self._stream_languages: dict[str, str] = {}
        self._stt_provider: Any = None
        self._stt_cfg: SttSection | None = None
        self._config_path: Path = config.config_path or Path("config.toml")

    # ================================================================ запуск

    async def start(self) -> None:
        cfg = self._cfg
        log.info("запуск speech-local")

        # 1. Хранилище: без него не существует ничего.
        self.db = Database(DbConfig(path=cfg.data_dir / "speech.db"))
        await self.db.start()
        await self.db.migrate(Path("migrations"))

        # 2. Профили: до любого компонента, который умеет ходить в облако.
        self.privacy = PrivacyController(
            cfg.default_profile, audit_writer=self._write_privacy_audit
        )
        self.privacy.add_listener(self._on_profile_switch)

        # 3. Очередь задач: восстановление незавершённого — внутри start().
        self.jobs = JobQueue(self.db, self.privacy, cfg.queue)
        self.jobs.register(JobType.STT, self._handle_stt_job)
        self.supersede = SupersedeService(self.db)

        # 3b. BYOK-хранилище ключей и gate доступности облака.
        self.keystore = KeyStore()
        self.offline = OfflineGate(OfflineConfig())

        # 3c. Текстовый провайдер перевода по конфигу.
        self._provider = self._build_provider()
        self.jobs.register(JobType.TRANSLATE, self._handle_translate)

        # 3d. Черновики: библиотека фактов, генератор, guard.
        from app.drafts.guardrails import DraftGuard, GuardConfig
        from app.drafts.library import FactLibrary
        from app.drafts.provider import DraftProvider, DraftProviderConfig
        self._library = FactLibrary(self.db)
        self._draft_guard = DraftGuard(self.db, GuardConfig())
        self._draft_provider = DraftProvider(
            provider=self._provider,
            library=self._library,
            config=DraftProviderConfig(),
        )
        self.jobs.register(JobType.DRAFT, self._handle_draft)

        # 3e. Транслятор черновиков (I4).
        from app.drafts.translate import DraftTranslator
        self._draft_translator = DraftTranslator(self._provider, self._draft_guard)

        # 4. STT: цепочка провайдеров по конфигу + один scheduler на процесс.
        self._stt_cfg = cfg.stt if cfg.stt is not None else default_stt_section()
        self.refresh_stt_keys()
        self._stt_provider = build_stt_chain(
            self._stt_cfg,
            privacy=self.privacy,
            keystore=self.keystore,
            secrets_dir=self._cfg.secrets_dir,
        )
        self.stt = SttScheduler(
            self._stt_provider,
            on_result=self._on_stt_result,
            on_error=self._on_stt_error,
            config=cfg.scheduler,
        )
        await self.stt.start()
        await self.jobs.start()

        log.info("ядро запущено, профиль: %s", self.privacy.profile.value)

        # 5. Захват и сегментация будут запущены при старте сессии.

        # 6. UI сервер (запускаем после ядра, чтобы snapshot работал)
        self.ui_server = UiServer(self, cfg.ui)
        await self.ui_server.start()
        log.info("UI сервер запущен")

    @property
    def library(self) -> Any:
        """Библиотека фактов (I1). Публичный доступ для UI-роутов (E7)."""
        return self._library

    def set_stream_language(self, role: str, source_language: str) -> None:
        """Меняет язык потока для СЛЕДУЮЩЕЙ сессии (E7 настройки).

        Применяется при следующем start_session: активную сессию не
        перенастраивает — сегментер и STT для роли уже запущены с
        зафиксированным на старте языком.
        """
        if role not in ("microphone", "meeting"):
            raise SpeechLocalError(f"неизвестная роль потока: {role}")
        if self._cfg.streams is None:
            self._cfg.streams = {}
        self._cfg.streams.setdefault(role, {})["source_language"] = source_language

    # ------------------------------------------------------ настройки STT (E7)

    @property
    def config(self) -> AppConfig:
        """Собранная конфигурация — для UI-роутов (GET /api/stt)."""
        return self._cfg

    @property
    def stt_config(self) -> SttSection | None:
        return self._stt_cfg

    @property
    def stt_provider(self) -> Any:
        """Активный STT-провайдер (для тестов hot-swap и диагностики)."""
        return self._stt_provider

    def refresh_stt_keys(self) -> None:
        """Подтянуть файловые ключи (~/.secrets/<key_name>) в KeyStore.

        Дашборд показывает `key_present` из KeyStore; при старте и смене
        цепочки ключи перечитываются с диска. Рантайм дополнительно
        подхватывает файл при истечении TTL (см. factory.key_provider).
        """
        if self.keystore is None or self._stt_cfg is None:
            return
        for entry in self._stt_cfg.chain:
            if entry.provider != "local_whisper" and entry.key_name:
                load_key_file(self.keystore, entry.key_name, self._cfg.secrets_dir)

    async def update_config(self, changes: dict[str, Any]) -> Any:
        """Применить изменения к config.toml (атомарная запись + валидация).

        Ручная правка файла и дашборд идут одним путём данных: оба ведут в
        один `Config`, второй источник истины не заводится.
        """
        from app.config import update as _update

        new_cfg = _update(self._config_path, changes)
        self._stt_cfg = new_cfg.stt
        return new_cfg

    async def reload_stt_provider(self, stt_cfg: SttSection | None = None) -> None:
        """Пересобрать активный STT-провайдер без перезапуска процесса.

        Job'ы типа 'stt', созданные до смены, при retry пойдут через новый
        провайдер — это ожидаемо: провайдер stateless per-request.
        """
        cfg = stt_cfg if stt_cfg is not None else self._stt_cfg
        if cfg is None:
            return
        self._stt_cfg = cfg
        self.refresh_stt_keys()
        new_provider = build_stt_chain(
            cfg,
            privacy=self.privacy,
            keystore=self.keystore,
            secrets_dir=self._cfg.secrets_dir,
        )
        old = self._stt_provider
        self._stt_provider = new_provider
        if self.stt is not None:
            old = self.stt.set_provider(new_provider)
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()

    async def start_session(self, meeting_title: str | None = None) -> str:
        """Начать сессию: запись в БД, захват, сегментация, конвейер."""
        assert self.db and self.privacy and self.stt
        if self._session_id is not None:
            raise SpeechLocalError("сессия уже идёт")

        session_id = uuid.uuid4().hex
        profile = self.privacy.profile
        await self.db.execute(
            """
            INSERT INTO sessions (id, started_at, meeting_title, status,
                                  default_privacy_profile, mode)
            VALUES (?, ?, ?, 'active', ?, 'live_safe')
            """,
            (session_id, _now_iso(), meeting_title, profile.value),
        )

        discovery = PipeWireDiscovery()
        self.capture = CaptureManager(discovery)
        session_dir = self._cfg.data_dir / "sessions" / session_id

        for role in ("microphone", "meeting"):
            settings = self._cfg.stream_settings(role)
            if not settings["enabled"]:
                continue
            stream_id = uuid.uuid4().hex
            await self.db.execute(
                """
                INSERT INTO audio_streams (id, session_id, role, source_language,
                                           target_language, pipewire_node, enabled,
                                           priority)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    stream_id, session_id, role,
                    settings["source_language"], settings["target_language"],
                    settings["node"],
                    "primary" if role == "microphone" else "secondary",
                ),
            )
            stream = self.capture.add(
                CaptureConfig(role=role, stable_key=settings["node"])  # type: ignore[arg-type]
            )
            segmenter = Segmenter(
                SegmentConfig(role=role, session_dir=session_dir)  # type: ignore[arg-type]
            )
            self._stream_languages[role] = settings["source_language"]
            self._segmenters[role] = segmenter
            self._pipelines.append(
                asyncio.create_task(
                    self._pipeline(role, stream_id, segmenter, stream),
                    name=f"pipeline:{role}",
                )
            )

        await self.capture.start_all()

        catch_up_count = await self.offline.catch_up(self.db, self.jobs)
        if catch_up_count:
            log.info("catch_up: %d отложенных переводов поставлены в очередь", catch_up_count)

        self._session_id = session_id
        log.info("сессия %s начата (%s)", session_id, profile.value)
        return session_id

    # ============================================================== конвейер

    async def _pipeline(
        self, role: str, stream_id: str, segmenter: Segmenter, stream
    ) -> None:
        """Захват → сегментация → (accurate: БД + STT) / (fast: быстрый трек)."""
        assert self.db and self.privacy and self.stt
        try:
            async for event in segmenter.run(stream):
                if isinstance(event, FinalSegment):
                    await self._accept_final(stream_id, event)
                elif isinstance(event, PartialUtterance):
                    await self._accept_partial(stream_id, event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — конвейер логирует и умирает явно
            log.exception("конвейер %s упал", role)

    async def _accept_final(self, stream_id: str, seg: FinalSegment) -> None:
        """Точный трек: запись в БД, постановка в STT."""
        assert self.db and self.privacy and self.stt and self.jobs
        await self.db.execute(
            """
            INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms,
                                  local_audio_path, privacy_profile, track,
                                  translation_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'accurate', 'pending', ?)
            """,
            (
                seg.id, self._session_id, stream_id,
                seg.t_start_ms, seg.t_end_ms, str(seg.audio_path),
                self.privacy.profile.value, _now_iso(),
            ),
        )
        if not self.stt.submit(seg):
            # Очередь переполнена: сегмент остаётся pending, jobs-очередь
            # доставит его в STT позже — WAV на диске, данные не потеряны.
            await self.jobs.enqueue(
                JobType.STT, segment_id=seg.id,
                payload={"audio_path": str(seg.audio_path),
                         "duration_ms": seg.duration_ms,
                         "role": seg.role},
                idempotency_key=f"stt:{seg.id}",
                delay_s=5.0,
            )

    async def _accept_partial(self, stream_id: str, part: PartialUtterance) -> None:
        """Быстрый трек. В MVP-срезе — только учёт; облачный realtime (D5)
        подключается сюда через PrivacyController.require(AUDIO_TO_CLOUD)."""
        # Намеренно пусто до задачи D5: частичные результаты не пишутся в БД
        # (инвариант 3) и без облачного провайдера им некуда идти.
        return

    # ---------------------------------------------------------- результаты STT

    async def _on_stt_result(self, seg: FinalSegment, result: SttResult) -> None:
        """Записать результат STT (текст + модель), запустить цепочку задач.

        Разбор JSON whisper — в провайдере (parser.py, задача C6); сюда
        приходит уже нормализованный `SttResult`.
        """
        assert self.db and self.supersede and self.jobs
        text = (result.raw_text or "").strip()

        def _tx(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE segments SET raw_text = ?, stt_model = ?, "
                "stt_provider_used = ? WHERE id = ?",
                (text or None, result.model, result.provider, seg.id),
            )

        await self.db.write(_tx)
        await self.supersede.link(seg.id)
        if text:
            await self.jobs.enqueue(
                JobType.TRANSLATE, segment_id=seg.id,
                idempotency_key=f"tr:{seg.id}",
            )

            # DRAFT: только вопросы собеседника (role='meeting').
            if seg.role == "meeting":
                from app.drafts.trigger import is_question
                lang = self._stream_languages.get("meeting", "en")
                is_q, _ = is_question(text, lang)
                if is_q:
                    await self.jobs.enqueue(
                        JobType.DRAFT, segment_id=seg.id,
                        idempotency_key=f"dr:{seg.id}",
                    )

    async def _on_stt_error(self, seg: FinalSegment, exc: BaseException) -> None:
        assert self.jobs
        if isinstance(exc, (NotImplementedError, SttChainExhausted)):
            # Постоянная ошибка: повтор не поможет и лишь зациклит очередь.
            # Поток записи при этом не останавливается (§8.7).
            log.error(
                "STT сегмента %s: постоянная ошибка, повтор не ставится: %s",
                seg.id, exc,
            )
            if self.db is not None:
                with contextlib.suppress(Exception):
                    await self.db.execute(
                        "UPDATE segments SET translation_status = 'skipped' WHERE id = ?",
                        (seg.id,),
                    )
            return
        await self.jobs.enqueue(
            JobType.STT, segment_id=seg.id,
            payload={"audio_path": str(seg.audio_path),
                     "duration_ms": seg.duration_ms, "role": seg.role},
            idempotency_key=f"stt:{seg.id}",
            delay_s=3.0,
        )

    async def _handle_stt_job(self, job) -> None:
        """Отложенный STT через jobs: переполнение очереди или ошибка."""
        assert self.stt
        p = job.payload
        seg = FinalSegment(
            id=job.segment_id,
            role=p["role"],
            t_start_ms=0, t_end_ms=int(p["duration_ms"]),
            audio_path=Path(p["audio_path"]),
            reason=None,  # type: ignore[arg-type]
            mean_level_db=0.0,
        )
        if not self.stt.submit(seg):
            raise SpeechLocalError("очередь STT всё ещё переполнена")

    # ============================================================ TRANSLATE

    def _build_provider(self) -> TranslationProvider:
        assert self.privacy and self.keystore
        active = self._cfg.provider.translation.active
        if active == "claude":
            return ClaudeTextProvider(
                privacy=self.privacy,
                key_provider=lambda: self.keystore.get("claude"),
                config=ClaudeConfig(),
            )
        return GeminiTextProvider(
            privacy=self.privacy,
            key_provider=lambda: self.keystore.get("gemini"),
        )

    async def _load_translate_input(
        self, segment_id: str
    ) -> tuple[str, str, str] | None:
        assert self.db
        row = await self.db.fetch_one(
            """
            SELECT s.raw_text, a.source_language, a.target_language
              FROM segments s
              JOIN audio_streams a ON a.id = s.stream_id
             WHERE s.id = ?
            """,
            (segment_id,),
        )
        if not row:
            return None
        raw_text = row["raw_text"]
        if not raw_text or not raw_text.strip():
            return None
        return raw_text.strip(), row["source_language"], row["target_language"]

    async def _handle_translate(self, job) -> None:
        assert self.db and self.offline and self._provider
        segment_id = job.segment_id
        if not segment_id:
            return

        provider = self._provider

        if not self.offline.should_attempt(provider.name):
            await self.jobs.enqueue(
                JobType.TRANSLATE, segment_id=segment_id,
                idempotency_key=f"tr:{segment_id}", delay_s=30.0,
            )
            return

        loaded = await self._load_translate_input(segment_id)
        if loaded is None:
            return
        raw_text, source_lang, target_lang = loaded

        from app.translation.context import build_context
        ctx = await build_context(self.db, segment_id, self._cfg.context)

        req = TranslationRequest(
            text=raw_text,
            source_language=source_lang,
            target_language=target_lang,
            mode=TranslationMode.LIVE_LITERAL,
            context=ctx,
            segment_id=segment_id,
        )

        try:
            result = await provider.translate(req, fence=job.fence)
        except StaleGenerationError:
            return
        except ProviderError as exc:
            self.offline.mark_unavailable(provider.name, exc)
            raise

        self.offline.mark_available(provider.name)
        clean = result.translation_clean
        raw = result.translation_raw

        def _tx(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE segments SET translation_raw = ?, "
                "translation_clean = ?, translation_status = 'done' "
                "WHERE id = ?",
                (raw, clean, segment_id),
            )

        await self.db.write(_tx)

        if self.ui_server:
            self.ui_server.publish(
                EventType.SEGMENT_TRANSLATED,
                {"segment_id": segment_id,
                 "translation": clean or raw,
                 "mode": req.mode.value,
                 "superseded_ids": []},
            )

    # ================================================================ DRAFT

    async def _load_draft_input(
        self, segment_id: str
    ) -> tuple[str, str] | None:
        """Загрузка raw_text + target_language для черновика.
        Черновик только на реплики собеседника (role='meeting')."""
        assert self.db
        row = await self.db.fetch_one(
            "SELECT s.raw_text, a.target_language, a.role "
            "FROM segments s JOIN audio_streams a ON a.id = s.stream_id "
            "WHERE s.id = ?",
            (segment_id,),
        )
        if not row:
            return None
        if row["role"] != "meeting":
            return None
        raw_text = row["raw_text"]
        if not raw_text or not raw_text.strip():
            return None
        return raw_text.strip(), row["target_language"]

    async def _handle_draft(self, job) -> None:
        """Обработчик JobType.DRAFT: I2 → I5."""
        assert self.db and self.offline and self._draft_provider and self._draft_guard and self._library
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

        loaded = await self._load_draft_input(segment_id)
        if loaded is None:
            return
        question_text, target_language = loaded

        session_id = self._session_id
        sess = await self.db.fetch_one(
            "SELECT library_context_id FROM sessions WHERE id = ?",
            (session_id,),
        )
        if not sess or not sess["library_context_id"]:
            return
        library_section_id = sess["library_context_id"]

        from app.drafts.provider import DraftRequest
        req = DraftRequest(
            session_id=session_id,
            trigger_segment_id=segment_id,
            question_text=question_text,
            target_language=target_language,
            library_section_id=library_section_id,
        )

        try:
            candidate = await self._draft_provider.generate(req, fence=job.fence)
        except StaleGenerationError:
            return
        except ProviderError as exc:
            self.offline.mark_unavailable(provider.name, exc)
            raise

        self.offline.mark_available(provider.name)
        if candidate is None:
            return

        ctx = await self._library.get(library_section_id)
        verdict = self._draft_guard.verify(candidate, ctx.content_text)
        draft_id = await self._draft_guard.store(candidate, verdict)
        if draft_id is None:
            return

        if self.ui_server:
            self.ui_server.publish(
                EventType.DRAFT_CREATED,
                {
                    "draft_id": draft_id,
                    "trigger_segment_id": segment_id,
                    "draft_ru": candidate.draft_ru,
                    "sources": list(candidate.sources),
                    "has_gaps": candidate.has_gaps_claimed,
                    "gap_note": candidate.gap_note,
                    "confidence": candidate.confidence,
                    "lang_ok": candidate.lang_ok,
                    "suggested_clarification": candidate.suggested_clarification,
                },
            )

        # I4: перевод черновика на язык встречи. Источник = язык генерации.
        # Недоступность перевода — ШТАТНЫЙ исход: черновик остаётся
        # на языке генерации, задачу не роняем.
        try:
            translated = await self._draft_translator.translate_draft(
                draft_id,
                candidate.draft_ru,
                candidate.target_language,
                fence=job.fence,
                source_language=req.generate_language,
            )
        except StaleGenerationError:
            translated = None
        except ProviderError as exc:
            self.offline.mark_unavailable(provider.name, exc)
            translated = None

        if translated is not None:
            await self._draft_guard.attach_translation(draft_id, translated)
            if self.ui_server:
                self.ui_server.publish(
                    EventType.DRAFT_TRANSLATED,
                    {"draft_id": draft_id, "draft_translated": translated},
                )

    # ============================================================== остановка

    async def stop_session(self) -> None:
        """Порядок из спеки §17. Каждый шаг переживает сбой предыдущего."""
        if self._session_id is None:
            return
        session_id = self._session_id
        log.info("остановка сессии %s", session_id)

        # 1. UI сервер остаётся — дашборд не падает между сессиями
        # 2. Стоп intake: захват перестаёт отдавать аудио.
        if self.capture is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.capture.stop_all(), timeout=3.0)

        # 3. Конвейеры дорабатывают буферы; flush хвоста внутри segmenter.run.
        for task in list(self._pipelines):
            task.cancel()
        for task in self._pipelines:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=2.0)
        self._pipelines.clear()
        self._segmenters.clear()
        self.capture = None

        # 4. STT остаётся запущенным между сессиями — только дождаться очереди

        # 5. Облачные сессии: teardown зарегистрированных хуков.
        if self.privacy is not None and self.privacy.profile is PrivacyProfile.OPEN:
            with contextlib.suppress(Exception):
                await self.privacy._teardown_all()  # noqa: SLF001 — санкционировано F3

        # 6. Финализация сессии в БД.
        if self.db is not None:
            with contextlib.suppress(Exception):
                await self.db.execute(
                    "UPDATE sessions SET ended_at = ?, status = 'finished' WHERE id = ?",
                    (_now_iso(), session_id),
                )
        self._session_id = None
        log.info("сессия %s завершена", session_id)

    async def shutdown(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        await self.stop_session()
        if self.jobs is not None:
            await self.jobs.stop()
        if self.db is not None:
            await self.db.close()  # дренаж писателя + checkpoint WAL внутри
        log.info("speech-local остановлен")

    # ============================================================ обслуживание

    async def _write_privacy_audit(self, fields: dict[str, Any]) -> None:
        assert self.db
        await self.db.execute(
            """
            INSERT INTO privacy_audit_log
                   (session_id, at, from_profile, to_profile, generation,
                    teardown_ms, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields.get("session_id") or self._session_id,
                _now_iso(),
                getattr(fields.get("from_profile"), "value", None),
                getattr(fields.get("to_profile"), "value", str(fields.get("to_profile"))),
                fields.get("generation", 0),
                fields.get("teardown_ms"),
                fields.get("reason"),
            ),
        )

    def _on_profile_switch(self, profile: PrivacyProfile, generation: int) -> None:
        """Синхронный слушатель: отмена очередных облачных аудио-задач."""
        if self.jobs is None:
            return
        if profile is PrivacyProfile.CONFIDENTIAL:
            asyncio.get_running_loop().create_task(
                self._cancel_cloud_jobs(), name="privacy-cancel-jobs"
            )

    async def _cancel_cloud_jobs(self) -> None:
        from app.privacy import Capability
        assert self.jobs
        await self.jobs.cancel_by_fence(Capability.AUDIO_TO_CLOUD)

    def snapshot(self) -> dict[str, Any]:
        """Сводка для /health и диагностического экрана."""
        return {
            "session_id": self._session_id,
            "privacy": self.privacy.snapshot() if self.privacy else None,
            "stt": self.stt.snapshot() if self.stt else None,
            "capture": self.capture.snapshot() if self.capture else None,
            "segmenters": {
                role: s.snapshot() for role, s in self._segmenters.items()
            },
            "db_writer": self.db.stats.snapshot() if self.db else None,
            "ui": (lambda s: s.get("ui", s) if isinstance(s, dict) and "ui" in s else s)(self.ui_server.snapshot()) if self.ui_server else None,
            "draft_provider": self._draft_provider.snapshot() if self._draft_provider else None,
        }

    # Удобный метод для проверки готовности (используется в UI /ready)
    async def is_ready(self) -> bool:
        """Возвращает True, если ядро запущено и готово принимать сессии."""
        return (
            self.db is not None
            and self.privacy is not None
            and self.jobs is not None
            and self.stt is not None
            and not self._stopping.is_set()
        )


# ==================================================================== запуск

async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    from pathlib import Path

    try:
        from app.config import load as _load_cfg
        from app.privacy import PrivacyProfile as _PrivacyProfile

        file_cfg = _load_cfg(Path("config.toml"))
        _streams: dict[str, dict[str, object]] = {}
        for _name, _sect in file_cfg.streams.items():
            _streams[_name] = {
                "source_language": _sect.source_language,
                "target_language": _sect.target_language,
                "node": _sect.pipewire_node,
                "enabled": _sect.enabled,
            }
            if _sect.priority:
                _streams[_name]["priority"] = _sect.priority
        _profile = _PrivacyProfile.CONFIDENTIAL if file_cfg.privacy.default_profile == "confidential" else _PrivacyProfile.OPEN
        _cfg = AppConfig(
            streams=_streams,
            default_profile=_profile,
            stt=file_cfg.stt,
            config_path=Path("config.toml"),
        )
    except Exception as _e:
        logging.getLogger(__name__).warning("config load failed, using defaults: %s", _e)
        _cfg = AppConfig(config_path=Path("config.toml"))
    app = Application(_cfg)
    await app.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        await app.shutdown()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
