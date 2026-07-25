"""app/audio/pcm.py — работа с сырым PCM.

Единый формат внутри проекта: PCM signed 16-bit little-endian, моно, 16 000 Гц.
Спека, раздел 7. Всё, что приходит от источника в другом формате, приводится
к этому виду на границе захвата и дальше по конвейеру не меняется.

Почему s16 mono 16k, а не float32: whisper.cpp принимает именно этот формат
без дополнительной конверсии, а объём вдвое меньше float32 — при удержании
30-60 секунд аудио в RAM (спека, раздел 5) это заметно для бюджета 2.1 ГБ.
"""

from __future__ import annotations

import array
import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SAMPLE_RATE: Final[int] = 16_000
CHANNELS: Final[int] = 1
SAMPLE_WIDTH: Final[int] = 2  # байт на отсчёт
BYTES_PER_SECOND: Final[int] = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

#: Длительность кадра анализа. 20 мс — компромисс: короче даёт шум в решениях
#: VAD, длиннее размывает границы речи и ухудшает точность таймкодов.
FRAME_MS: Final[int] = 20
FRAME_SAMPLES: Final[int] = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES: Final[int] = FRAME_SAMPLES * SAMPLE_WIDTH


@dataclass(frozen=True, slots=True)
class Frame:
    """Кадр фиксированной длины с абсолютным временем от начала потока."""

    pcm: bytes
    t_start_ms: int

    @property
    def t_end_ms(self) -> int:
        return self.t_start_ms + FRAME_MS

    @property
    def rms(self) -> float:
        return rms_s16(self.pcm)

    @property
    def dbfs(self) -> float:
        return dbfs_s16(self.pcm)


def bytes_to_ms(n: int) -> int:
    return n * 1000 // BYTES_PER_SECOND


def ms_to_bytes(ms: int) -> int:
    """Округляет вниз до границы отсчёта: половина отсчёта ломает выравнивание."""
    raw = ms * BYTES_PER_SECOND // 1000
    return raw - (raw % SAMPLE_WIDTH)


def rms_s16(pcm: bytes) -> float:
    """Среднеквадратичный уровень, нормированный к 1.0.

    array используется вместо struct.unpack в цикле: на кадрах 20 мс разница
    невелика, но VAD вызывает это на каждом кадре непрерывно, и на часовой
    сессии набегает 180 000 вызовов на поток.
    """
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return math.sqrt(total / len(samples)) / 32768.0


def dbfs_s16(pcm: bytes) -> float:
    """Уровень в dBFS. Тишина отдаёт -100, а не -inf: удобнее для графиков."""
    r = rms_s16(pcm)
    if r <= 1e-9:
        return -100.0
    return max(-100.0, 20 * math.log10(r))


def peak_s16(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    return max(abs(s) for s in samples) / 32768.0


class FrameSplitter:
    """Режет непрерывный поток байтов на кадры фиксированной длины.

    Источник отдаёт куски произвольного размера, не кратные кадру. Хвост
    сохраняется до следующего вызова: терять его нельзя, это разрыв в звуке,
    который VAD воспримет как границу речи.
    """

    __slots__ = ("_tail", "_offset_bytes")

    def __init__(self) -> None:
        self._tail = b""
        self._offset_bytes = 0

    def push(self, chunk: bytes) -> list[Frame]:
        buf = self._tail + chunk
        frames: list[Frame] = []
        pos = 0
        while len(buf) - pos >= FRAME_BYTES:
            frames.append(
                Frame(
                    pcm=buf[pos : pos + FRAME_BYTES],
                    t_start_ms=bytes_to_ms(self._offset_bytes),
                )
            )
            pos += FRAME_BYTES
            self._offset_bytes += FRAME_BYTES
        self._tail = buf[pos:]
        return frames

    def flush(self) -> Frame | None:
        """Добить хвост нулями и отдать последним кадром при закрытии потока."""
        if not self._tail:
            return None
        padded = self._tail + b"\x00" * (FRAME_BYTES - len(self._tail))
        frame = Frame(pcm=padded, t_start_ms=bytes_to_ms(self._offset_bytes))
        self._offset_bytes += len(self._tail)
        self._tail = b""
        return frame

    def reset(self, offset_ms: int = 0) -> None:
        """Сброс после реконнекта. Смещение сохраняется, чтобы таймкоды
        оставались абсолютными относительно старта сессии."""
        self._tail = b""
        self._offset_bytes = ms_to_bytes(offset_ms)

    @property
    def position_ms(self) -> int:
        return bytes_to_ms(self._offset_bytes)


def write_wav(path: Path, pcm: bytes) -> None:
    """Записать WAV. whisper-cli принимает файл, а не поток (спека, раздел 7)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    # Атомарная публикация: STT-воркер не увидит недописанный файл.
    tmp.replace(path)


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        if (
            wf.getnchannels() != CHANNELS
            or wf.getsampwidth() != SAMPLE_WIDTH
            or wf.getframerate() != SAMPLE_RATE
        ):
            raise ValueError(
                f"{path}: ожидался {SAMPLE_RATE} Гц / {CHANNELS} кан. / "
                f"{SAMPLE_WIDTH * 8} бит"
            )
        return wf.readframes(wf.getnframes())


def wav_header(data_size: int) -> bytes:
    """Заголовок WAV для потоковой отдачи без промежуточного файла."""
    return b"".join(
        (
            b"RIFF",
            struct.pack("<I", 36 + data_size),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, CHANNELS, SAMPLE_RATE,
                        BYTES_PER_SECOND, CHANNELS * SAMPLE_WIDTH, 16),
            b"data",
            struct.pack("<I", data_size),
        )
    )
