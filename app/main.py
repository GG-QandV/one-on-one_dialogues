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
import json
import logging
import signal
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.audio.capture import CaptureConfig, CaptureManager, StreamRole
from app.audio.discovery import PipeWireDiscovery
from app.audio.segmenter import (
    FinalSegment,
    PartialUtterance,
    SegmentConfig,
    Segmenter,
)
from app.db import Database, DbConfig
from app.errors import SpeechLocalError
from app.privacy import PrivacyController, PrivacyProfile
from app.queue import JobQueue, JobType, QueueConfig
from app.stt.fallback import ModelSelector
from app.stt.runner import WhisperConfig, WhisperRawResult, WhisperRunner
from app.stt.scheduler import SchedulerConfig, SttScheduler
from app.translation.supersede import SupersedeService
from app.ui.server import UiServer, UiConfig

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Собранная конфигурация. Наполняется из config.toml модулем config.py
    (задача B1, middle); здесь — только структура и дефолты для сборки."""
    data_dir: Path = Path("data")
    whisper: WhisperConfig = WhisperConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    queue: QueueConfig = QueueConfig()
    default_profile: PrivacyProfile = PrivacyProfile.OPEN
    streams: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    ui: UiConfig = UiConfig()  # UI server configuration

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

        # 4. STT: один runner, один selector, один scheduler на процесс.
        runner = WhisperRunner(cfg.whisper)
        selector = ModelSelector(
            cfg.whisper.model_path, cfg.whisper.fallback_model_path
        )
        self.stt = SttScheduler(
            runner, selector,
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
        await discovery.refresh()
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
            self._segmenters[role] = segmenter
            self._pipelines.append(
                asyncio.create_task(
                    self._pipeline(role, stream_id, segmenter, stream),
                    name=f"pipeline:{role}",
                )
            )

        await self.capture.start_all()
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

    async def _on_stt_result(
        self, seg: FinalSegment, raw: WhisperRawResult
    ) -> None:
        """Минимальная запись результата.

        Полный разбор JSON (текст, язык, avg_logprob) — parser.py, задача C6
        (middle). Здесь пишется сырой текст, чтобы вертикальный срез был
        замкнут до появления парсера; после C6 эта функция делегирует ему.
        """
        assert self.db and self.supersede and self.jobs
        text = " ".join(
            s.get("text", "").strip()
            for s in raw.payload.get("transcription", [])
        ).strip()

        def _tx(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE segments SET raw_text = ?, stt_model = ? WHERE id = ?",
                (text or None, raw.model_used, seg.id),
            )

        await self.db.write(_tx)
        await self.supersede.link(seg.id)
        if text:
            await self.jobs.enqueue(
                JobType.TRANSLATE, segment_id=seg.id,
                idempotency_key=f"tr:{seg.id}",
            )

    async def _on_stt_error(self, seg: FinalSegment, exc: BaseException) -> None:
        assert self.jobs
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

    # ============================================================== остановка

    async def stop_session(self) -> None:
        """Порядок из спеки §17. Каждый шаг переживает сбой предыдущего."""
        if self._session_id is None:
            return
        session_id = self._session_id
        log.info("остановка сессии %s", session_id)

        # 1. Остановить UI сервер первым (спека E1 пункт 10)
        if self.ui_server:
            await self.ui_server.stop()
            self.ui_server = None

        # 2. Стоп intake: захват перестаёт отдавать аудио.
        if self.capture is not None:
            with contextlib.suppress(Exception):
                await self.capture.stop_all()

        # 3. Конвейеры дорабатывают буферы; flush хвоста внутри segmenter.run.
        for task in self._pipelines:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=10.0)
        self._pipelines.clear()
        self._segmenters.clear()

        # 4. STT дорабатывает очередь (все WAV уже записаны).
        if self.stt is not None:
            await self.stt.stop()

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
            "ui": self.ui_server.snapshot() if self.ui_server else None,
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
    app = Application(AppConfig())
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