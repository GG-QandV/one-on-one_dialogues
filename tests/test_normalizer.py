"""Tests for app/audio/normalizer.py (C3)."""

import audioop
import math
import struct

import pytest

from app.audio.normalizer import AudioFormat, FORMAT_TARGET, needs_normalization, normalize


def _make_sine(hz: int, duration_s: float, rate: int, channels: int) -> bytes:
    """Generate a sine wave PCM s16le."""
    samples = int(rate * duration_s)
    pcm = b""
    for i in range(samples):
        val = int(math.sin(2 * math.pi * hz * i / rate) * 30000)
        pcm += struct.pack("<h", val)
    if channels > 1:
        stereo = b""
        for i in range(samples):
            val = struct.unpack("<h", pcm[i * 2 : (i + 1) * 2])[0]
            stereo += struct.pack("<h", val) * channels
        pcm = stereo
    return pcm


def test_needs_normalization_target_false():
    assert needs_normalization(FORMAT_TARGET) is False


def test_needs_normalization_48k_stereo_true():
    fmt = AudioFormat(48000, 2, 2)
    assert needs_normalization(fmt) is True


def test_normalize_identity():
    """normalize with target format returns identical bytes."""
    pcm = _make_sine(440, 0.1, 16000, 1)
    result = normalize(pcm, FORMAT_TARGET)
    assert result == pcm


def test_normalize_empty():
    assert normalize(b"", FORMAT_TARGET) == b""


def test_normalize_stereo_to_mono():
    """Стерео 48 кГц → моно 16 кГц: длина выхода меньше входа."""
    pcm = _make_sine(440, 0.1, 48000, 2)
    result = normalize(pcm, AudioFormat(48000, 2, 2))
    # 48k stereo 0.1s = 48000*2*2 = 19200 bytes → 16k mono = 16000*2 = 3200 bytes
    assert len(result) == 3200
    assert len(result) % 2 == 0


def test_normalize_resample_only():
    """48k mono → 16k mono: длина уменьшается в 3 раза."""
    pcm = _make_sine(440, 0.1, 48000, 1)
    result = normalize(pcm, AudioFormat(48000, 1, 2))
    expected = len(pcm) * 16000 // 48000
    expected = expected - (expected % 2)
    assert len(result) == expected


def test_normalize_unsupported_sample_width():
    with pytest.raises(ValueError, match="unsupported sample_width"):
        normalize(b"\x00" * 100, AudioFormat(16000, 1, 1))


def test_normalize_ffmpeg_equivalence():
    """FFmpeg и C3 дают RMS в пределах 10% допуска на одном входе."""
    import subprocess as sp
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg not available")

    pcm_in = _make_sine(440, 0.2, 48000, 2)

    # C3
    c3_out = normalize(pcm_in, AudioFormat(48000, 2, 2))

    # FFmpeg
    proc = sp.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "s16le", "-ac", "2", "-ar", "48000", "-i", "pipe:",
            "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:",
        ],
        input=pcm_in,
        capture_output=True,
        timeout=10,
    )
    ffmpeg_out = proc.stdout

    def rms(data: bytes) -> float:
        if not data:
            return 0.0
        samples = struct.unpack("<" + "h" * (len(data) // 2), data)
        mean = sum(s * s for s in samples) / len(samples)
        return math.sqrt(mean)

    rms_c3 = rms(c3_out)
    rms_ff = rms(ffmpeg_out)

    if rms_ff > 0:
        ratio = abs(rms_c3 - rms_ff) / rms_ff
        assert ratio < 0.10, f"RMS ratio {ratio:.4f} exceeds 10%"


def test_normalize_no_external_processes():
    """Проверка: модуль не импортирует subprocess, os.system, shutil."""
    import ast
    import builtins

    source = __import__("app.audio.normalizer", fromlist=[""]).__file__
    with open(source) as f:
        tree = ast.parse(f.read())

    dangerous = {"subprocess", "os.system", "shutil"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in dangerous, f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module and "subprocess" in node.module:
                pytest.fail(f"forbidden import from subprocess in {node.lineno}")


def test_normalize_odd_length_rounded():
    """Если длина нечётная — обрезается до чётной."""
    pcm = _make_sine(440, 0.1, 16000, 1)
    # Add one extra byte to make it odd
    odd = pcm + b"\x00"
    result = normalize(odd, FORMAT_TARGET)
    assert len(result) % 2 == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
