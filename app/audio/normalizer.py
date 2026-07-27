"""app/audio/normalizer.py — C3 PCM normalizer (Python fallback без FFmpeg).

Третичное звено нормализации: pw-record → FFmpeg → C3.
Чистый Python-ресемплинг через audioop, без внешних процессов.
"""

from __future__ import annotations

import audioop
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AudioFormat:
    sample_rate: int
    channels: int
    sample_width: int


FORMAT_TARGET = AudioFormat(16000, 1, 2)


def needs_normalization(fmt: AudioFormat) -> bool:
    return fmt != FORMAT_TARGET


def normalize(pcm: bytes, src_fmt: AudioFormat) -> bytes:
    if src_fmt.sample_width != 2:
        raise ValueError(f"unsupported sample_width: {src_fmt.sample_width}, only 2 (s16le)")
    if not pcm:
        return b""
    result = pcm
    sw = 2
    ch = src_fmt.channels
    sr = src_fmt.sample_rate

    if ch != 1:
        result = audioop.tomono(result, sw, 0.5, 0.5)
        ch = 1
    if sr != FORMAT_TARGET.sample_rate:
        result, _state = audioop.ratecv(
            result, sw, ch, sr, FORMAT_TARGET.sample_rate, None
        )
    result = result[: len(result) - (len(result) % 2)]
    return result
