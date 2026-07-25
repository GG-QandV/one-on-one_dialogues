"""app/stt/scheduler.py — общая очередь к одному экземпляру whisper. C4b.

Спека: раздел 7 «один экземпляр whisper.cpp, два потока последовательно»,
раздел 5 «не более 30-60 с необработанного аудио в RAM» (здесь очередь
файловая, поэтому ограничение переносится на глубину очереди), раздел 15
«очередь STT растёт → деградация».

Закрытие риска R2
-----------------
Оба потока (microphone, meeting) кладут сегменты в одну priority-очередь.
Одновременно исполняется ровно один вызов whisper — это инвариант,
обеспеченный устройством цикла, а не блокировкой: воркер один.

Приоритет
---------
Исходящий поток (microphone) — основной по спеке (70% трафика): при
конкуренции его сегменты обрабатываются раньше. Внутри приоритета — FIFO
по времени начала, чтобы не переупорядочивать реплики одного говорящего.

Backpressure
------------
Очередь ограничена по суммарной длительности аудио. При превышении мягкого
порога поднимается флаг backlog — его читает каскад деградации (F2a: короче
сегменты, отключить частичные). При жёстком пороге новые сегменты не
принимаются с ошибкой — сегментатор продолжает писать WAV на диск, данные
не теряются (приоритет спеки: сохранность выше live).
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.audio.segmenter import FinalSegment
from app.errors import SttError
from app.stt.fallback import ModelSelector
from app.stt.runner import WhisperRawResult, WhisperRunner

log = logging.getLogger(__name__)

#: Приоритет ролей: меньше — раньше. Спека, раздел 4.
_ROLE_PRIORITY = {"microphone": 0, "meeting": 1}


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    #: Мягкий порог очереди: поднимается флаг backlog.
    backlog_soft_ms: int = 30_000
    #: Жёсткий порог: новые сегменты отклоняются (WAV остаётся на диске).
    backlog_hard_ms: int = 60_000
    #: Язык на роль: {'microphone': 'ru', 'meeting': 'en'} либо 'auto'.
    language_by_role: dict[str, str] = field(default_factory=dict)


#: Колбэк готового результата: (сегмент, сырой результат whisper).
ResultSink = Callable[[FinalSegment, WhisperRawResult], Awaitable[None]]
#: Колбэк ошибки: (сегмент, исключение).
ErrorSink = Callable[[FinalSegment, BaseException], Awaitable[None]]


@dataclass(order=True)
class _Entry:
    priority: int
    t_start_ms: int
    seq: int
    segment: FinalSegment = field(compare=False)


class SttScheduler:
    """Единственный потребитель whisper. Один воркер по построению."""

    def __init__(
        self,
        runner: WhisperRunner,
        selector: ModelSelector,
        on_result: ResultSink,
        on_error: ErrorSink,
        config: SchedulerConfig | None = None,
    ) -> None:
        self._runner = runner
        self._selector = selector
        self._on_result = on_result
        self._on_error = on_error
        self._cfg = config or SchedulerConfig()

        self._heap: list[_Entry] = []
        self._seq = 0
        self._queued_ms = 0
        self._wakeup = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._inflight: FinalSegment | None = None

        self.processed = 0
        self.rejected = 0
        self.errors = 0
        self._last_rtf: float | None = None

    # ------------------------------------------------------------- постановка

    @property
    def backlog_ms(self) -> int:
        inflight = self._inflight.duration_ms if self._inflight else 0
        return self._queued_ms + inflight

    @property
    def backlogged(self) -> bool:
        """Флаг для каскада деградации (F2a)."""
        return self.backlog_ms >= self._cfg.backlog_soft_ms

    def submit(self, segment: FinalSegment) -> bool:
        """Поставить сегмент. False — очередь переполнена, сегмент НЕ принят.

        Отклонение не теряет данные: WAV уже на диске, вызывающий обязан
        оставить запись в БД со статусом pending и повторить позже
        (этим занимается обработчик задач очереди jobs).
        """
        if self.backlog_ms + segment.duration_ms > self._cfg.backlog_hard_ms:
            self.rejected += 1
            log.error(
                "очередь STT переполнена (%d мс), сегмент %s отклонён",
                self.backlog_ms, segment.id,
            )
            return False

        self._seq += 1
        heapq.heappush(
            self._heap,
            _Entry(
                priority=_ROLE_PRIORITY.get(segment.role, 9),
                t_start_ms=segment.t_start_ms,
                seq=self._seq,
                segment=segment,
            ),
        )
        self._queued_ms += segment.duration_ms
        self._wakeup.set()
        if self.backlogged:
            log.warning("очередь STT: %d мс (мягкий порог %d)",
                        self.backlog_ms, self._cfg.backlog_soft_ms)
        return True

    # ------------------------------------------------------------ жизненный цикл

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="stt-scheduler")

    async def stop(self, timeout_s: float = 30.0) -> None:
        """Дорабатывает текущий вызов и очередь, затем выходит (F3).

        Очередь дорабатывается, а не бросается: сегменты уже записаны,
        их распознавание — часть graceful shutdown. Таймаут страхует от
        зависшего whisper.
        """
        self._stop.set()
        self._wakeup.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout_s)
            except TimeoutError:
                log.error("scheduler не завершился за %.0f с, отменяю", timeout_s)
                self._task.cancel()
            self._task = None

    # ------------------------------------------------------------------ цикл

    async def _loop(self) -> None:
        while True:
            if not self._heap:
                if self._stop.is_set():
                    return
                self._wakeup.clear()
                await self._wakeup.wait()
                continue

            entry = heapq.heappop(self._heap)
            segment = entry.segment
            self._queued_ms -= segment.duration_ms
            self._inflight = segment
            try:
                await self._process(segment)
            finally:
                self._inflight = None

    async def _process(self, segment: FinalSegment) -> None:
        language = self._cfg.language_by_role.get(segment.role, "auto")
        model_path = self._selector.current_path
        try:
            result = await self._runner.transcribe(
                segment.audio_path,
                segment.duration_ms,
                model_path=model_path,
                language=language,
            )
        except BaseException as exc:  # noqa: BLE001 — воркер один, падать нельзя
            self.errors += 1
            log.error("STT сегмента %s не удался: %s", segment.id, exc)
            try:
                await self._on_error(segment, exc)
            except Exception:  # noqa: BLE001
                log.exception("обработчик ошибки STT сам упал")
            return

        self.processed += 1
        self._last_rtf = result.realtime_factor
        # Наблюдение питает fallback: следующий вызов может пойти на tiny.
        self._selector.observe(result.realtime_factor)
        try:
            await self._on_result(segment, result)
        except Exception:  # noqa: BLE001
            log.exception("обработчик результата STT упал (сегмент %s)", segment.id)

    # ------------------------------------------------------------ наблюдаемость

    def snapshot(self) -> dict[str, Any]:
        """Диагностический экран (E5) и каскад деградации (F2)."""
        return {
            "backlog_ms": self.backlog_ms,
            "backlogged": self.backlogged,
            "queue_depth": len(self._heap),
            "inflight": self._inflight.id if self._inflight else None,
            "processed": self.processed,
            "rejected": self.rejected,
            "errors": self.errors,
            "last_rtf": round(self._last_rtf, 2) if self._last_rtf else None,
            "model": self._selector.snapshot(),
        }
