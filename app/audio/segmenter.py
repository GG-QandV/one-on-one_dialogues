"""app/audio/segmenter.py — сегментация речи. Задачи C5b и C5c.

Спека: раздел 5 (пауза 800-1200 мс, минимум 0.8 с, принудительная резка 15 с,
частичные результаты каждые 500-800 мс), раздел 3 (два трека).

Что делает модуль
-----------------
Превращает непрерывный поток кадров в две сущности:

  * ``FinalSegment`` — закрытая реплика, записанная в WAV. Уходит в точный
    трек: локальный whisper, ``raw_text``, экспорт.
  * ``PartialUtterance`` — накопленное с начала текущей реплики аудио,
    выдаётся периодически, пока человек говорит. Уходит в быстрый трек,
    в БД как ``raw_text`` не пишется никогда (инвариант 3 спеки).

Четыре причины закрыть сегмент
------------------------------
1. **Пауза.** Основной путь. Порог из конфига, входит в бюджет задержки
   целиком — см. таблицу в разделе 5 спеки.
2. **Предел длины.** Человек говорит без пауз дольше лимита. Резка идёт не
   ровно по таймеру, а по локальному минимуму энергии в конце окна: это
   заметно снижает шанс разрезать слово пополам.
3. **Разрыв записи.** ``GapEvent`` от захвата означает дыру в аудио.
   Сегмент, внутри которого дыра, даст неверные таймкоды и склеенные слова,
   поэтому закрывается принудительно до разрыва.
4. **Остановка сессии.** Хвост не выбрасывается: незаконченная реплика
   закрывается и отправляется в обработку.

Предзапись
----------
Кольцевой буфер держит последние 300 мс до начала речи. VAD принимает решение
задним числом, и без предзаписи начало первого слова обрезается. Дешёвая
защита: 300 мс — это 9.6 КБ.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from app.audio.capture import AudioChunk, GapEvent, StreamRole
from app.audio.pcm import (
    FRAME_MS,
    Frame,
    FrameSplitter,
    bytes_to_ms,
    ms_to_bytes,
    write_wav,
)
from app.audio.vad import VadConfig, VadDetector, VadEventType

log = logging.getLogger(__name__)


class CloseReason(str, Enum):
    PAUSE = "pause"
    MAX_LENGTH = "max_length"
    GAP = "gap"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class SegmentConfig:
    """Секция [vad] config.toml плюс параметры сегментации."""

    role: StreamRole
    session_dir: Path

    #: Порог паузы для закрытия. Спека: 800-1200 мс.
    silence_close_ms: int = 800
    #: Короче этого сегмент не отдаётся: щелчки и вдохи не должны идти в STT.
    min_segment_ms: int = 800
    #: Предел длины. Спека: 15 с.
    max_segment_ms: int = 15_000
    #: Окно поиска тихого места при принудительной резке.
    cut_search_ms: int = 700
    #: Предзапись до начала речи.
    preroll_ms: int = 300
    #: Сколько тишины оставить после последнего речевого кадра. Остальное
    #: обрезается: пауза закрытия не должна уходить в whisper.
    tail_keep_ms: int = 200
    #: Периодичность частичных результатов. Спека: 500-800 мс.
    partial_interval_ms: int = 600
    #: Не отдавать частичный результат короче этого — нечего распознавать.
    partial_min_ms: int = 400
    #: Ограничение памяти на одну незакрытую реплику.
    hard_buffer_ms: int = 30_000

    @property
    def preroll_frames(self) -> int:
        return max(0, self.preroll_ms // FRAME_MS)


@dataclass(frozen=True, slots=True)
class FinalSegment:
    """Закрытая реплика. Точный трек."""

    id: str
    role: StreamRole
    t_start_ms: int
    t_end_ms: int
    audio_path: Path
    reason: CloseReason
    #: Средний уровень в dBFS — для диагностики «почему whisper дал мусор».
    mean_level_db: float

    @property
    def duration_ms(self) -> int:
        return self.t_end_ms - self.t_start_ms


@dataclass(frozen=True, slots=True)
class PartialUtterance:
    """Промежуточное состояние текущей реплики. Быстрый трек.

    Аудио передаётся в памяти: файл не пишется. Частичные результаты
    появляются несколько раз в секунду, и запись каждого на диск дала бы
    лишний ввод-вывод ради данных, которые будут выброшены через 600 мс.
    """

    utterance_id: str
    role: StreamRole
    t_start_ms: int
    t_end_ms: int
    pcm: bytes
    sequence: int

    @property
    def duration_ms(self) -> int:
        return self.t_end_ms - self.t_start_ms


SegmenterEvent = FinalSegment | PartialUtterance


@dataclass
class SegmenterStats:
    segments_closed: int = 0
    partials_emitted: int = 0
    by_reason: dict[str, int] = None  # type: ignore[assignment]
    dropped_too_short: int = 0
    forced_cuts: int = 0
    buffer_ms: int = 0
    longest_segment_ms: int = 0

    def __post_init__(self) -> None:
        if self.by_reason is None:
            self.by_reason = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "segments_closed": self.segments_closed,
            "partials_emitted": self.partials_emitted,
            "by_reason": dict(self.by_reason),
            "dropped_too_short": self.dropped_too_short,
            "forced_cuts": self.forced_cuts,
            "buffer_ms": self.buffer_ms,
            "longest_segment_ms": self.longest_segment_ms,
        }


class Segmenter:
    """Сегментация одного потока. Экземпляр на поток, состояние не общее.

    Использование::

        seg = Segmenter(SegmentConfig(role="microphone", session_dir=d))
        async for event in seg.run(capture_stream):
            match event:
                case FinalSegment(): ...   # -> очередь STT
                case PartialUtterance(): ...  # -> быстрый трек
    """

    def __init__(
        self,
        config: SegmentConfig,
        *,
        vad_config: VadConfig | None = None,
    ) -> None:
        self._cfg = config
        self._vad = VadDetector(vad_config)
        self._splitter = FrameSplitter()

        self._preroll: deque[Frame] = deque(maxlen=max(1, config.preroll_frames))
        self._buffer: list[Frame] = []
        self._segment_start_ms: int | None = None
        self._utterance_id: str | None = None
        self._partial_seq = 0
        self._last_partial_ms = 0
        self._levels: list[float] = []

        self.stats = SegmenterStats()

    # ------------------------------------------------------------- главный цикл

    async def run(self, source) -> Any:  # AsyncIterator[SegmenterEvent]
        """Асинхронный генератор событий поверх потока захвата."""
        async for item in source:
            if isinstance(item, GapEvent):
                for event in await self._on_gap(item):
                    yield event
                continue
            for event in await self._on_chunk(item):
                yield event

        for event in await self.flush(CloseReason.SHUTDOWN):
            yield event

    # ------------------------------------------------------------- обработка

    async def _on_chunk(self, chunk: AudioChunk) -> list[SegmenterEvent]:
        events: list[SegmenterEvent] = []
        for frame in self._splitter.push(chunk.pcm):
            events.extend(await self._on_frame(frame))
        return events

    async def _on_frame(self, frame: Frame) -> list[SegmenterEvent]:
        events: list[SegmenterEvent] = []
        vad_events = self._vad.process(frame)

        for ev in vad_events:
            if ev.type is VadEventType.SPEECH_START and self._segment_start_ms is None:
                self._open(ev.at_ms)

        if self._segment_start_ms is None:
            # Тишина: копим предзапись и уходим.
            self._preroll.append(frame)
            return events

        self._buffer.append(frame)
        self._levels.append(frame.dbfs)
        self.stats.buffer_ms = len(self._buffer) * FRAME_MS

        # 1. Предел длины важнее паузы: буфер не должен расти неограниченно.
        if self._duration_ms() >= self._cfg.max_segment_ms:
            events.append(await self._close(CloseReason.MAX_LENGTH))
            return [e for e in events if e is not None]

        # 2. Аварийный предел на случай, если max_segment_ms задан абсурдно.
        if self._duration_ms() >= self._cfg.hard_buffer_ms:
            log.error(
                "буфер реплики достиг %d мс — принудительное закрытие",
                self._duration_ms(),
            )
            events.append(await self._close(CloseReason.MAX_LENGTH))
            return [e for e in events if e is not None]

        # 3. Пауза.
        silence_ms = self._vad.silence_duration_ms(frame.t_end_ms)
        if not self._vad.in_speech and silence_ms >= self._cfg.silence_close_ms:
            events.append(await self._close(CloseReason.PAUSE))
            return [e for e in events if e is not None]

        # 4. Частичный результат для быстрого трека.
        partial = self._maybe_partial(frame)
        if partial is not None:
            events.append(partial)

        return events

    async def _on_gap(self, gap: GapEvent) -> list[SegmenterEvent]:
        """Разрыв записи: закрыть текущую реплику, сбросить состояние.

        Оставлять дыру внутри сегмента нельзя: whisper склеит слова по обе
        стороны разрыва и выдаст таймкоды, не соответствующие реальности.
        """
        log.warning(
            "разрыв записи %s: %d мс в позиции %d (%s)",
            gap.role, gap.duration_ms, gap.at_ms, gap.reason,
        )
        events: list[SegmenterEvent] = []
        closed = await self._close(CloseReason.GAP)
        if closed is not None:
            events.append(closed)

        # Длинный разрыв означает смену устройства — фон надо пересчитать.
        self._vad.reset(recalibrate=gap.duration_ms > 1000)
        self._splitter.reset(offset_ms=gap.at_ms + gap.duration_ms)
        self._preroll.clear()
        return events

    async def flush(self, reason: CloseReason = CloseReason.SHUTDOWN) -> list[SegmenterEvent]:
        """Закрыть незавершённую реплику при остановке (задача F3)."""
        tail = self._splitter.flush()
        if tail is not None and self._segment_start_ms is not None:
            self._buffer.append(tail)
        closed = await self._close(reason)
        return [closed] if closed is not None else []

    # ------------------------------------------------------- открытие/закрытие

    def _open(self, at_ms: int) -> None:
        self._segment_start_ms = at_ms
        self._utterance_id = uuid.uuid4().hex
        self._partial_seq = 0
        self._last_partial_ms = at_ms
        self._levels = []
        # Предзапись переносится в буфер: без неё срезается начало слова.
        self._buffer = list(self._preroll)
        self._preroll.clear()
        log.debug("%s: открыта реплика на %d мс", self._cfg.role, at_ms)

    async def _close(self, reason: CloseReason) -> FinalSegment | None:
        if self._segment_start_ms is None or not self._buffer:
            self._reset_segment()
            return None

        frames = self._buffer
        if reason is CloseReason.MAX_LENGTH:
            frames, leftover = self._split_at_quietest(frames)
            self.stats.forced_cuts += 1
        else:
            leftover = []
            frames = self._trim_tail(frames)

        duration_ms = len(frames) * FRAME_MS
        start_ms = self._segment_start_ms

        if duration_ms < self._cfg.min_segment_ms:
            self.stats.dropped_too_short += 1
            log.debug(
                "%s: реплика %d мс отброшена (минимум %d)",
                self._cfg.role, duration_ms, self._cfg.min_segment_ms,
            )
            self._reset_segment()
            self._restore_leftover(leftover, start_ms + duration_ms)
            return None

        pcm = b"".join(f.pcm for f in frames)
        segment_id = uuid.uuid4().hex
        path = self._cfg.session_dir / self._cfg.role / f"{start_ms:09d}_{segment_id[:8]}.wav"

        # Запись блокирующая: в event loop её держать нельзя, иначе на время
        # записи встают оба потока захвата.
        await asyncio.get_running_loop().run_in_executor(None, write_wav, path, pcm)

        mean_db = sum(self._levels) / len(self._levels) if self._levels else -100.0
        segment = FinalSegment(
            id=segment_id,
            role=self._cfg.role,
            t_start_ms=start_ms,
            t_end_ms=start_ms + duration_ms,
            audio_path=path,
            reason=reason,
            mean_level_db=mean_db,
        )

        self.stats.segments_closed += 1
        self.stats.by_reason[reason.value] = self.stats.by_reason.get(reason.value, 0) + 1
        self.stats.longest_segment_ms = max(self.stats.longest_segment_ms, duration_ms)

        log.info(
            "%s: сегмент %d-%d мс (%d мс, %s, %.1f dBFS)",
            self._cfg.role, segment.t_start_ms, segment.t_end_ms,
            duration_ms, reason.value, mean_db,
        )

        self._reset_segment()
        self._restore_leftover(leftover, start_ms + duration_ms)
        return segment

    def _trim_tail(self, frames: list[Frame]) -> list[Frame]:
        """Убрать хвостовую тишину, оставив tail_keep_ms.

        Сегмент закрывается по паузе в 800-1200 мс, и вся эта пауза лежит в
        буфере. Отправлять её в whisper — значит распознавать на секунду
        больше аудио в каждом сегменте. При медиане реплики 3-5 секунд это
        20-30% лишней работы STT и столько же лишней задержки.

        Обрезка идёт до последнего речевого кадра плюс запас: резать вплотную
        нельзя, whisper теряет окончания слов без короткого затухания.
        """
        last_active = self._vad.last_active_ms
        if last_active is None or not frames:
            return frames
        keep_until = last_active + self._cfg.tail_keep_ms
        trimmed = [f for f in frames if f.t_start_ms < keep_until]
        if len(trimmed) < len(frames):
            log.debug(
                "%s: хвост обрезан на %d мс",
                self._cfg.role, (len(frames) - len(trimmed)) * FRAME_MS,
            )
        return trimmed or frames

    def _split_at_quietest(self, frames: list[Frame]) -> tuple[list[Frame], list[Frame]]:
        """Разрезать по локальному минимуму энергии в конце окна.

        Резка ровно по таймеру попадает в середину слова примерно всегда.
        Поиск самого тихого кадра в последних cut_search_ms даёт границу,
        которая чаще совпадает с межсловным промежутком.
        """
        window = max(1, self._cfg.cut_search_ms // FRAME_MS)
        if len(frames) <= window + 1:
            return frames, []
        tail = frames[-window:]
        offset = len(frames) - window
        quietest = min(range(len(tail)), key=lambda i: tail[i].dbfs)
        cut = offset + quietest + 1
        return frames[:cut], frames[cut:]

    def _restore_leftover(self, leftover: list[Frame], at_ms: int) -> None:
        """Хвост после принудительной резки открывает следующую реплику."""
        if not leftover:
            return
        self._segment_start_ms = at_ms
        self._utterance_id = uuid.uuid4().hex
        self._partial_seq = 0
        self._last_partial_ms = at_ms
        self._buffer = leftover
        self._levels = [f.dbfs for f in leftover]

    def _reset_segment(self) -> None:
        self._segment_start_ms = None
        self._utterance_id = None
        self._buffer = []
        self._levels = []
        self.stats.buffer_ms = 0

    # ------------------------------------------------------- частичные результаты

    def _maybe_partial(self, frame: Frame) -> PartialUtterance | None:
        if self._segment_start_ms is None or self._utterance_id is None:
            return None
        if frame.t_end_ms - self._last_partial_ms < self._cfg.partial_interval_ms:
            return None
        duration = self._duration_ms()
        if duration < self._cfg.partial_min_ms:
            return None

        self._last_partial_ms = frame.t_end_ms
        self._partial_seq += 1
        self.stats.partials_emitted += 1

        # Копия буфера целиком, а не приращение: быстрый трек распознаёт
        # реплику с начала, иначе на стыках теряются слова.
        return PartialUtterance(
            utterance_id=self._utterance_id,
            role=self._cfg.role,
            t_start_ms=self._segment_start_ms,
            t_end_ms=self._segment_start_ms + duration,
            pcm=b"".join(f.pcm for f in self._buffer),
            sequence=self._partial_seq,
        )

    # ------------------------------------------------------------ служебное

    def _duration_ms(self) -> int:
        return len(self._buffer) * FRAME_MS

    def snapshot(self) -> dict[str, Any]:
        """Для диагностического экрана (E5)."""
        return {
            "role": self._cfg.role,
            "segmenter": self.stats.snapshot(),
            "vad": self._vad.stats.snapshot(),
            "open_segment_ms": self._duration_ms() if self._segment_start_ms else 0,
            "position_ms": self._splitter.position_ms,
        }


def bytes_for_ms(ms: int) -> int:
    """Реэкспорт для тестов, чтобы не тянуть pcm в тестовый модуль."""
    return ms_to_bytes(ms)


def ms_for_bytes(n: int) -> int:
    return bytes_to_ms(n)
