from __future__ import annotations

from pathlib import Path

import struct

import pytest

from app.audio.pcm import FRAME_BYTES, Frame
from app.audio.vad import VadConfig, VadDetector, VadEventType, VadState
from app.audio.capture import AudioChunk, GapEvent
from app.audio.segmenter import (
    CloseReason,
    FinalSegment,
    PartialUtterance,
    SegmentConfig,
    Segmenter,
)

FRAME_MS = 20
SILENCE = b"\x00" * FRAME_BYTES
LOUD = struct.pack("<h", 32767) * (FRAME_BYTES // 2)


def _frame(pcm: bytes, i: int) -> Frame:
    return Frame(pcm=pcm, t_start_ms=i * FRAME_MS)


def _chunk(pcm: bytes, t_ms: int, role: str = "microphone") -> AudioChunk:
    rms_val = 0.0 if pcm == SILENCE else 0.5
    return AudioChunk(
        role=role, pcm=pcm, t_start_ms=t_ms, duration_ms=20, rms=rms_val
    )


async def _agen(*items):
    for i in items:
        yield i


def _cfg(*, cal=50, debounce=40, hangover=60, **kw) -> VadConfig:
    """Short timescales for fast tests."""
    return VadConfig(
        calibration_ms=cal,
        onset_debounce_ms=debounce,
        hangover_ms=hangover,
        **kw,
    )


class TestVadDetector:

    def test_calibrates_and_detects_speech(self):
        vad = VadDetector(_cfg(cal=50, debounce=40, hangover=40))
        # Calibration (3 frames)
        sil = [_frame(SILENCE, i) for i in range(5)]
        # Speech (15 frames = 300ms, well past debounce of 2 frames)
        sp = [_frame(LOUD, 5 + i) for i in range(15)]

        events = []
        for f in sil + sp:
            events.extend(vad.process(f))

        starts = [e for e in events if e.type is VadEventType.SPEECH_START]
        assert len(starts) == 1
        assert vad.in_speech is True

    def test_speech_end_fires_after_hangover(self):
        vad = VadDetector(_cfg(cal=50, debounce=40, hangover=60))
        # Calibrate + speech
        for i in range(5):
            vad.process(_frame(SILENCE, i))
        for i in range(5, 15):
            vad.process(_frame(LOUD, i))

        # Silence frames — within hangover at first
        for i in range(15, 17):
            events = vad.process(_frame(SILENCE, i))
            assert vad.in_speech is True

        # After hangover expires — SPEECH_END
        all_events = []
        for i in range(17, 22):
            all_events.extend(vad.process(_frame(SILENCE, i)))
        ends = [e for e in all_events if e.type is VadEventType.SPEECH_END]
        assert len(ends) >= 1


class TestSegmenter:

    @pytest.mark.asyncio
    async def test_splits_by_silence(self, tmp_path):
        cfg = SegmentConfig(
            role="microphone",
            session_dir=Path(tmp_path),
            silence_close_ms=400,
            min_segment_ms=100,
        )
        seg = Segmenter(cfg, vad_config=_cfg(cal=50, debounce=40, hangover=40))

        silence = [_chunk(SILENCE, i * 20) for i in range(10)]
        speech = [_chunk(LOUD, (10 + i) * 20) for i in range(20)]
        pause = [_chunk(SILENCE, (30 + i) * 20) for i in range(30)]

        events = []
        async for ev in seg.run(_agen(*silence, *speech, *pause)):
            events.append(ev)

        finals = [e for e in events if isinstance(e, FinalSegment)]
        assert len(finals) >= 1

    @pytest.mark.asyncio
    async def test_partial_emitted_at_interval(self, tmp_path):
        cfg = SegmentConfig(
            role="microphone",
            session_dir=Path(tmp_path),
            partial_interval_ms=200,
            partial_min_ms=100,
            min_segment_ms=100,
            silence_close_ms=10000,
        )
        seg = Segmenter(cfg, vad_config=_cfg(cal=50, debounce=40, hangover=40))

        speech = [_chunk(LOUD, (10 + i) * 20) for i in range(40)]
        silence = [_chunk(SILENCE, i * 20) for i in range(10)]

        events = []
        async for ev in seg.run(_agen(*silence, *speech)):
            events.append(ev)
            if isinstance(ev, FinalSegment):
                break

        partials = [e for e in events if isinstance(e, PartialUtterance)]
        assert len(partials) >= 1
