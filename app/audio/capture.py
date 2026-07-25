"""app/audio/capture.py — захват аудиопотоков. Задача C2 (C2a + C2b).

Спека: раздел 4 «Направления», раздел 7 «Стек», раздел 15 «Деградация».

Два независимых потока
----------------------
`mic_audio` (70% трафика) и `meeting_audio` (30%) захватываются раздельно и
никогда не смешиваются. Каждый — свой процесс захвата, свой таймлайн, свой
язык. Один экземпляр whisper обслуживает оба последовательно (спека, раздел 7),
но это забота планировщика STT (C4b), не захвата.

Выбор бэкенда
-------------
Основной — ``pw-record``: он нативен для PipeWire и умеет привести формат
прямо в графе (``--rate 16000 --channels 1 --format s16``), то есть
нормализация происходит без отдельного процесса FFmpeg. Это экономит
~40-80 МБ RSS и одно межпроцессное копирование на каждый поток при бюджете
2.1 ГБ.

Резервный — FFmpeg через pipewire-pulse. Нужен там, где ``pw-record``
отсутствует или отказывается работать с конкретным узлом. Переключение
автоматическое, факт переключения виден в диагностике.

Таймлайн
--------
Метки времени считаются от количества выданных байт, а не от системных часов:
``t_ms = bytes_emitted / bytes_per_ms``. Часы дрейфуют и прыгают при
синхронизации NTP, а счётчик байт монотонен и точно соответствует аудио.

При обрыве и переподключении реальное время идёт, а байты не поступают.
Разрыв фиксируется явно: ``timeline_offset_ms`` увеличивается на длительность
провала, а потребитель получает ``GapEvent``. Без этого таймкоды в SRT
«съедут» на всю длительность обрыва.

Обратное давление
-----------------
Очередь чанков ограничена. Приоритет сохранности данных над скоростью
(спека, раздел 15) означает: при заполнении очереди мы не выбрасываем аудио
молча. Политика задаётся явно и по умолчанию — ``BLOCK``: чтение из процесса
приостанавливается, буфер трубы наполняется, PipeWire сообщает об xrun.
Это видно в диагностике и честнее тихой потери речи.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from app.audio.discovery import AudioNode, NodeKind, PipeWireDiscovery
from app.errors import AudioError, CaptureInterrupted, NodeNotFound

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2                    # s16le
BYTES_PER_MS = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH / 1000  # 32.0

StreamRole = Literal["meeting", "microphone"]


class OverflowPolicy(str, Enum):
    BLOCK = "block"      # приостановить чтение, дать обратное давление
    DROP_OLDEST = "drop_oldest"  # только для диагностических потоков


class CaptureState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """Кусок нормализованного PCM.

    ``t_start_ms`` — позиция на таймлайне сессии с учётом разрывов.
    """

    role: StreamRole
    pcm: bytes
    t_start_ms: int
    duration_ms: int
    rms: float

    @property
    def t_end_ms(self) -> int:
        return self.t_start_ms + self.duration_ms


@dataclass(frozen=True, slots=True)
class GapEvent:
    """Провал в записи: обрыв захвата, смена устройства, xrun."""

    role: StreamRole
    at_ms: int
    duration_ms: int
    reason: str


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    role: StreamRole
    stable_key: str                  # ключ узла из discovery
    chunk_ms: int = 20
    queue_max_chunks: int = 500      # 10 секунд при chunk_ms=20
    overflow: OverflowPolicy = OverflowPolicy.BLOCK
    reconnect_base_s: float = 0.5
    reconnect_max_s: float = 15.0
    #: Считать поток мёртвым, если байты не идут дольше этого времени.
    stall_timeout_s: float = 3.0

    @property
    def chunk_bytes(self) -> int:
        return int(self.chunk_ms * BYTES_PER_MS)


@dataclass
class CaptureStats:
    """Для диагностического экрана (E5)."""

    state: CaptureState = CaptureState.IDLE
    backend: str = "-"
    node_id: int | None = None
    bytes_captured: int = 0
    timeline_offset_ms: int = 0
    reconnects: int = 0
    gaps_ms_total: int = 0
    queue_depth: int = 0
    last_rms: float = 0.0
    last_error: str | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "backend": self.backend,
            "node_id": self.node_id,
            "seconds_captured": round(self.bytes_captured / (BYTES_PER_MS * 1000), 1),
            "timeline_offset_ms": self.timeline_offset_ms,
            "reconnects": self.reconnects,
            "gaps_ms_total": self.gaps_ms_total,
            "queue_depth": self.queue_depth,
            "last_rms": round(self.last_rms, 4),
            "last_error": self.last_error,
        }


def _rms_s16le(pcm: bytes) -> float:
    """Среднеквадратичный уровень, нормированный к 0..1.

    Реализовано без numpy: numpy тянет ~30 МБ RSS на процесс, а нужна одна
    свёртка по буферу в 640 байт каждые 20 мс. Собственный цикл на срезах
    memoryview укладывается в единицы микросекунд и не расширяет зависимости.
    """
    if not pcm:
        return 0.0
    view = memoryview(pcm).cast("h")  # int16, порядок байт платформы (LE на x86)
    total = 0
    for sample in view:
        total += sample * sample
    mean = total / len(view)
    return (mean ** 0.5) / 32768.0


class _Backend:
    """Описание способа запуска захвата."""

    name: str = "-"

    def command(self, node: AudioNode) -> list[str]:  # pragma: no cover - интерфейс
        raise NotImplementedError

    @staticmethod
    def available() -> bool:  # pragma: no cover - интерфейс
        raise NotImplementedError


class PwRecordBackend(_Backend):
    name = "pw-record"

    @staticmethod
    def available() -> bool:
        return shutil.which("pw-record") is not None

    def command(self, node: AudioNode) -> list[str]:
        return [
            "pw-record",
            f"--target={node.node_id}",
            "--rate", str(SAMPLE_RATE),
            "--channels", str(CHANNELS),
            "--format", "s16",
            "--latency", "20ms",
            "-",  # в stdout
        ]


class FfmpegPulseBackend(_Backend):
    """Резерв: FFmpeg через слой совместимости pipewire-pulse."""

    name = "ffmpeg"

    @staticmethod
    def available() -> bool:
        return shutil.which("ffmpeg") is not None

    def command(self, node: AudioNode) -> list[str]:
        source = node.name
        if node.kind is NodeKind.SINK_MONITOR and not source.endswith(".monitor"):
            source = f"{source}.monitor"
        return [
            "ffmpeg",
            "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "pulse", "-i", source,
            "-ac", str(CHANNELS),
            "-ar", str(SAMPLE_RATE),
            "-f", "s16le",
            "-",
        ]


class CaptureStream:
    """Супервизор одного аудиопотока.

    Держит дочерний процесс захвата, следит за его живостью, переподключается
    при обрыве и отдаёт нормализованные чанки через асинхронный итератор.
    """

    def __init__(
        self,
        config: CaptureConfig,
        discovery: PipeWireDiscovery,
    ) -> None:
        self._cfg = config
        self._discovery = discovery
        self._queue: asyncio.Queue[AudioChunk | GapEvent | None] = asyncio.Queue(
            maxsize=config.queue_max_chunks
        )
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._bytes_emitted = 0
        self._timeline_offset_ms = 0
        self.stats = CaptureStats()

    # ------------------------------------------------------------ управление

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self.stats.state = CaptureState.STARTING
        self._task = asyncio.create_task(
            self._supervise(), name=f"capture:{self._cfg.role}"
        )

    async def stop(self, timeout_s: float = 3.0) -> None:
        self._stop.set()
        await self._kill_process()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout_s)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        await self._queue.put(None)   # сентинел для итератора
        self.stats.state = CaptureState.STOPPED

    def __aiter__(self) -> AsyncIterator[AudioChunk | GapEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[AudioChunk | GapEvent]:
        while True:
            item = await self._queue.get()
            self.stats.queue_depth = self._queue.qsize()
            if item is None:
                return
            yield item

    # ------------------------------------------------------------- супервизор

    async def _supervise(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            gap_started = time.monotonic()
            try:
                node = await self._resolve_node()
                backend = self._pick_backend()
                self.stats.backend = backend.name
                self.stats.node_id = node.node_id

                if attempt > 0:
                    gap_ms = int((time.monotonic() - gap_started) * 1000)
                    await self._register_gap(gap_ms, "reconnect")

                self.stats.state = CaptureState.RUNNING
                self.stats.last_error = None
                attempt = 0
                await self._pump(node, backend)

                if self._stop.is_set():
                    break
                # Процесс завершился сам — это обрыв, а не штатный выход.
                raise CaptureInterrupted("процесс захвата завершился")

            except asyncio.CancelledError:
                raise
            except (NodeNotFound, AudioError, CaptureInterrupted) as exc:
                if self._stop.is_set():
                    break
                attempt += 1
                self.stats.reconnects += 1
                self.stats.state = CaptureState.RECONNECTING
                self.stats.last_error = str(exc)[:200]
                delay = min(
                    self._cfg.reconnect_base_s * (2 ** (attempt - 1)),
                    self._cfg.reconnect_max_s,
                )
                log.warning(
                    "[%s] захват прерван (%s), попытка %d через %.1f с",
                    self._cfg.role, exc, attempt, delay,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
            except Exception:  # noqa: BLE001 — супервизор не умирает
                log.exception("[%s] непредвиденный сбой захвата", self._cfg.role)
                self.stats.state = CaptureState.FAILED
                await asyncio.sleep(self._cfg.reconnect_max_s)

        await self._kill_process()

    async def _resolve_node(self) -> AudioNode:
        node = await self._discovery.try_resolve(self._cfg.stable_key)
        if node is None:
            raise NodeNotFound(
                f"узел '{self._cfg.stable_key}' не найден; "
                f"проверьте настройку в панели или инструкцию virtual sink"
            )
        return node

    def _pick_backend(self) -> _Backend:
        if PwRecordBackend.available():
            return PwRecordBackend()
        if FfmpegPulseBackend.available():
            log.warning("[%s] pw-record недоступен, резерв FFmpeg", self._cfg.role)
            return FfmpegPulseBackend()
        raise AudioError("нет ни pw-record, ни ffmpeg: захват невозможен")

    # ------------------------------------------------------------------ насос

    async def _pump(self, node: AudioNode, backend: _Backend) -> None:
        cmd = backend.command(node)
        log.info("[%s] запуск захвата: %s", self._cfg.role, " ".join(cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._proc.stdout is not None

        stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"capture-stderr:{self._cfg.role}"
        )
        chunk_bytes = self._cfg.chunk_bytes
        try:
            while not self._stop.is_set():
                try:
                    data = await asyncio.wait_for(
                        self._proc.stdout.readexactly(chunk_bytes),
                        timeout=self._cfg.stall_timeout_s,
                    )
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        await self._emit(exc.partial)
                    raise CaptureInterrupted("поток закрылся") from exc
                except TimeoutError as exc:
                    raise CaptureInterrupted(
                        f"нет данных дольше {self._cfg.stall_timeout_s} с"
                    ) from exc
                await self._emit(data)
        finally:
            stderr_task.cancel()
            await self._kill_process()

    async def _emit(self, pcm: bytes) -> None:
        duration_ms = int(len(pcm) / BYTES_PER_MS)
        t_start = self._timeline_offset_ms + int(self._bytes_emitted / BYTES_PER_MS)
        rms = _rms_s16le(pcm)

        chunk = AudioChunk(
            role=self._cfg.role,
            pcm=pcm,
            t_start_ms=t_start,
            duration_ms=duration_ms,
            rms=rms,
        )
        self._bytes_emitted += len(pcm)
        self.stats.bytes_captured += len(pcm)
        self.stats.last_rms = rms

        if self._cfg.overflow is OverflowPolicy.BLOCK:
            # Обратное давление: ждём место в очереди. Чтение из stdout
            # приостанавливается, наполняется буфер трубы, дальше PipeWire
            # зафиксирует xrun — это видно и честно.
            if self._queue.full():
                log.warning("[%s] очередь захвата заполнена, обратное давление",
                            self._cfg.role)
            await self._queue.put(chunk)
        else:
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                    self.stats.gaps_ms_total += duration_ms
                except asyncio.QueueEmpty:
                    pass
            self._queue.put_nowait(chunk)

        self.stats.queue_depth = self._queue.qsize()

    async def _register_gap(self, gap_ms: int, reason: str) -> None:
        """Зафиксировать провал и сдвинуть таймлайн.

        Без сдвига все последующие таймкоды окажутся раньше реального времени
        ровно на длительность обрыва, и субтитры разъедутся.
        """
        if gap_ms <= 0:
            return
        at_ms = self._timeline_offset_ms + int(self._bytes_emitted / BYTES_PER_MS)
        self._timeline_offset_ms += gap_ms
        self.stats.timeline_offset_ms = self._timeline_offset_ms
        self.stats.gaps_ms_total += gap_ms
        log.warning("[%s] провал %d мс на отметке %d мс (%s)",
                    self._cfg.role, gap_ms, at_ms, reason)
        await self._queue.put(
            GapEvent(role=self._cfg.role, at_ms=at_ms, duration_ms=gap_ms, reason=reason)
        )

    async def _drain_stderr(self) -> None:
        """stderr бэкенда — источник диагностики xrun, не мусор."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                if text:
                    log.debug("[%s] backend: %s", self._cfg.role, text)
        except asyncio.CancelledError:
            return

    async def _kill_process(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except TimeoutError:
            log.warning("[%s] процесс захвата не завершился, kill", self._cfg.role)
            proc.kill()
            await proc.wait()


class CaptureManager:
    """Оба потока сразу. Единственная точка старта и остановки захвата."""

    def __init__(self, discovery: PipeWireDiscovery) -> None:
        self._discovery = discovery
        self._streams: dict[StreamRole, CaptureStream] = {}

    def add(self, config: CaptureConfig) -> CaptureStream:
        if config.role in self._streams:
            raise ValueError(f"поток '{config.role}' уже добавлен")
        stream = CaptureStream(config, self._discovery)
        self._streams[config.role] = stream
        return stream

    def get(self, role: StreamRole) -> CaptureStream:
        return self._streams[role]

    async def start_all(self) -> None:
        await self._discovery.start()
        for stream in self._streams.values():
            await stream.start()

    async def stop_all(self) -> None:
        await asyncio.gather(
            *(s.stop() for s in self._streams.values()), return_exceptions=True
        )
        await self._discovery.stop()

    def snapshot(self) -> dict[str, object]:
        return {role: s.stats.snapshot() for role, s in self._streams.items()}
