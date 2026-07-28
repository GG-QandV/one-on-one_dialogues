#!/usr/bin/env python3
"""C0 — диагностический spike на живом железе.

Цель: на целевой машине (Ryzen 5 5600U, CPU-only) эмпирически измерить,
какая модель whisper.cpp тянет real-time для ДВУХ потоков через один
экземпляр, и сколько ест RAM. Результат — вердикт по каждой модели, а не
обещания из карточек.

Что делает:
  1. Проверяет пререквизиты (whisper-cli, pw-record/wpctl, ffmpeg).
  2. Перечисляет аудио-узлы PipeWire (источники mic/meeting).
  3. Обеспечивает наличие GGML-моделей (скачивает из официального
     ggerganov/whisper.cpp, если нет).
  4. Замеряет RTF (real-time factor) и RSS каждой модели на 10-с аудио.
  5. Проверяет одновременный захват двух потоков (pw-record).
  6. Печатает таблицу-вердикт.

ВАЖНО: скрипт не проверялся на живом железе автором. Запускать на целевой
машине. Требует установленного whisper.cpp (whisper-cli) и PipeWire.

Использование:
    python3 c0_spike.py                       # полный прогон, синтетика
    python3 c0_spike.py --wav sample10s.wav   # замер на своей записи 10с
    python3 c0_spike.py --threads 6           # число потоков whisper
    python3 c0_spike.py --skip-capture        # без проверки захвата
    python3 c0_spike.py --models base small    # свой набор моделей
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------- модели

# Официальный источник: ggerganov/whisper.cpp на HuggingFace.
# Имена файлов — как в download-ggml-model.sh. Кванты (q5_1/q5_0/q8_0)
# СВЕРИТЬ в актуальном репо: набор квантов иногда меняется.
HF_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

# Финалисты расчёта: base (fast), small (accurate-фаворит), кванты, turbo
# (проверка гипотезы), tiny (нижний fallback).
DEFAULT_MODELS: dict[str, str] = {
    "tiny": "ggml-tiny.bin",
    "base": "ggml-base.bin",
    "small": "ggml-small.bin",
    "small-q5_1": "ggml-small-q5_1.bin",
    "large-v3-turbo": "ggml-large-v3-turbo.bin",
    "large-v3-turbo-q5_0": "ggml-large-v3-turbo-q5_0.bin",
}

# Пороги вердикта по RTF (real-time factor = длительность аудио / время
# обработки). Два потока идут через один экземпляр последовательно, поэтому
# для их real-time нужен запас >= 2x плюс overhead.
RTF_COMFORTABLE = 3.0   # >= : тянет 2 потока с запасом
RTF_TIGHT = 2.0         # >= : впритык (один поток / только accurate-трек)

TEST_AUDIO_SECONDS = 10
SAMPLE_RATE = 16_000


# --------------------------------------------------------------- утилиты

def _log(msg: str) -> None:
    print(msg, flush=True)


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


# --------------------------------------------------------------- 1. пререквизиты

@dataclass
class Prereqs:
    whisper_cli: str | None
    has_pw_record: bool
    has_wpctl: bool
    has_pw_dump: bool
    has_ffmpeg: bool
    has_usr_time: bool


def check_prereqs() -> Prereqs:
    _log("== 1. Пререквизиты ==")
    # whisper-cli может называться по-разному в разных сборках.
    whisper = None
    for name in ("whisper-cli", "whisper", "main"):
        if _have(name):
            whisper = shutil.which(name)
            break
    p = Prereqs(
        whisper_cli=whisper,
        has_pw_record=_have("pw-record"),
        has_wpctl=_have("wpctl"),
        has_pw_dump=_have("pw-dump"),
        has_ffmpeg=_have("ffmpeg"),
        has_usr_time=Path("/usr/bin/time").exists(),
    )
    _log(f"  whisper-cli : {p.whisper_cli or 'НЕ НАЙДЕН — установите whisper.cpp'}")
    _log(f"  pw-record   : {'да' if p.has_pw_record else 'нет'}")
    _log(f"  wpctl       : {'да' if p.has_wpctl else 'нет'}")
    _log(f"  pw-dump     : {'да' if p.has_pw_dump else 'нет'}")
    _log(f"  ffmpeg      : {'да' if p.has_ffmpeg else 'нет (резервный бэкенд)'}")
    _log(f"  /usr/bin/time: {'да' if p.has_usr_time else 'нет (RSS через ps)'}")
    if not p.whisper_cli:
        _log("  ! Без whisper-cli замер моделей невозможен.")
    return p


# --------------------------------------------------------------- 2. узлы PipeWire

def discover_nodes(p: Prereqs) -> None:
    _log("\n== 2. Аудио-узлы PipeWire ==")
    if p.has_wpctl:
        res = _run(["wpctl", "status"], timeout=10)
        if res.returncode == 0:
            # Печатаем секцию Sources/Sinks как есть — оператору виднее.
            _log(res.stdout)
            return
    if p.has_pw_dump:
        res = _run(["pw-dump"], timeout=10)
        if res.returncode == 0:
            names = re.findall(r'"node\.description"\s*:\s*"([^"]+)"', res.stdout)
            if names:
                _log("  Узлы (node.description):")
                for n in sorted(set(names)):
                    _log(f"    - {n}")
                return
    _log("  ! Не удалось перечислить узлы (нет wpctl/pw-dump или пустой вывод).")


# --------------------------------------------------------------- 3. модели

def ensure_models(models: dict[str, str], models_dir: Path) -> dict[str, Path]:
    _log("\n== 3. Модели GGML ==")
    models_dir.mkdir(parents=True, exist_ok=True)
    available: dict[str, Path] = {}
    for key, fname in models.items():
        dest = models_dir / fname
        if dest.exists() and dest.stat().st_size > 0:
            _log(f"  {key}: есть ({dest.stat().st_size // (1024*1024)} МБ)")
            available[key] = dest
            continue
        url = f"{HF_BASE}/{fname}"
        _log(f"  {key}: качаю {url}")
        ok = _download(url, dest)
        if ok:
            available[key] = dest
        else:
            _log(f"    ! Не удалось скачать {fname} — пропуск. "
                 f"Сверьте имя кванта в ggerganov/whisper.cpp.")
    if not available:
        _log("  ! Ни одной модели — замер невозможен.")
    return available


def _download(url: str, dest: Path) -> bool:
    tmp = dest.with_suffix(dest.suffix + ".part")
    if _have("curl"):
        res = _run(["curl", "-L", "-f", "-o", str(tmp), url], timeout=1800)
        ok = res.returncode == 0
    elif _have("wget"):
        res = _run(["wget", "-O", str(tmp), url], timeout=1800)
        ok = res.returncode == 0
    else:
        _log("    ! Нет ни curl, ни wget.")
        return False
    if ok and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(dest)
        return True
    if tmp.exists():
        tmp.unlink()
    return False


# --------------------------------------------------------------- 4. тест-аудио

def make_synthetic_wav(path: Path, seconds: int = TEST_AUDIO_SECONDS) -> Path:
    """Синтетический сигнал для замера СКОРОСТИ (не качества).

    Смесь тонов в речевом диапазоне + лёгкий шум: тишина обрабатывается
    whisper нерепрезентативно быстро, поэтому не годится для замера RTF.
    ВНИМАНИЕ: WER на этом сигнале не измеряют — для качества нужна речь.
    """
    n = SAMPLE_RATE * seconds
    frames = bytearray()
    for i in range(n):
        t = i / SAMPLE_RATE
        # три форманто-подобных тона + шум
        v = (
            0.35 * math.sin(2 * math.pi * 220 * t)
            + 0.25 * math.sin(2 * math.pi * 700 * t)
            + 0.15 * math.sin(2 * math.pi * 2600 * t)
        )
        v += 0.05 * ((i * 2654435761) % 1000 / 1000 - 0.5)  # дешёвый псевдошум
        s = max(-1.0, min(1.0, v))
        frames += struct.pack("<h", int(s * 30000))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))
    return path


# --------------------------------------------------------------- 5. замер

@dataclass
class BenchResult:
    model: str
    ok: bool
    proc_seconds: float = 0.0
    rtf: float = 0.0
    rss_mb: int = 0
    note: str = ""


def bench_model(
    key: str, model_path: Path, wav: Path, whisper_cli: str,
    threads: int, audio_seconds: float, has_usr_time: bool,
) -> BenchResult:
    cmd_core = [
        whisper_cli, "-m", str(model_path), "-f", str(wav),
        "-t", str(threads), "-nt", "-l", "auto",
    ]
    # RSS через /usr/bin/time -v, если есть; иначе без RSS.
    if has_usr_time:
        cmd = ["/usr/bin/time", "-v", *cmd_core]
    else:
        cmd = cmd_core

    t0 = time.monotonic()
    try:
        res = _run(cmd, timeout=600)
    except subprocess.TimeoutExpired:
        return BenchResult(key, ok=False, note="timeout >600s (слишком медленно)")
    dt = time.monotonic() - t0

    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()[-1:] or ["ошибка"]
        return BenchResult(key, ok=False, note=f"exit {res.returncode}: {tail[0][:60]}")

    rss_mb = 0
    if has_usr_time:
        m = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", res.stderr)
        if m:
            rss_mb = int(m.group(1)) // 1024

    rtf = audio_seconds / dt if dt > 0 else 0.0
    return BenchResult(key, ok=True, proc_seconds=dt, rtf=rtf, rss_mb=rss_mb)


def verdict(rtf: float) -> str:
    if rtf >= RTF_COMFORTABLE:
        return "✅ тянет 2 потока"
    if rtf >= RTF_TIGHT:
        return "⚠️ впритык (1 поток / accurate)"
    return "❌ не real-time для 2 потоков"


# --------------------------------------------------------------- 6. захват

def check_dual_capture(p: Prereqs, out_dir: Path, seconds: int = 3) -> None:
    _log("\n== 6. Одновременный захват двух потоков ==")
    if not p.has_pw_record:
        _log("  ! pw-record не найден — проверка захвата пропущена.")
        return
    # Захватываем дефолтный источник дважды параллельно как прокси
    # «двух потоков»: в проде mic и meeting — разные узлы, но здесь
    # цель — убедиться, что одновременный захват в целевой формат работает.
    a = out_dir / "capture_a.wav"
    b = out_dir / "capture_b.wav"
    fmt = ["--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16"]
    try:
        pa = subprocess.Popen(["pw-record", *fmt, str(a)])
        pb = subprocess.Popen(["pw-record", *fmt, str(b)])
        time.sleep(seconds)
    finally:
        for proc in (locals().get("pa"), locals().get("pb")):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
    for label, f in (("A", a), ("B", b)):
        size = f.stat().st_size if f.exists() else 0
        status = "ок" if size > 1000 else "ПУСТО — поток не пишется"
        _log(f"  поток {label}: {size} байт — {status}")


# --------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="C0 spike: whisper RTF/RSS + PipeWire")
    ap.add_argument("--wav", type=Path, help="своя запись 10с (иначе синтетика)")
    ap.add_argument("--threads", type=int, default=6, help="потоки whisper (Ryzen 5 5600U: 6)")
    ap.add_argument("--models-dir", type=Path, default=Path("./models"))
    ap.add_argument("--models", nargs="*", help="подмножество ключей моделей")
    ap.add_argument("--skip-capture", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=Path("./c0_out"))
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    _log("C0 spike — диагностика железа под whisper.cpp\n")
    p = check_prereqs()
    discover_nodes(p)

    if not p.whisper_cli:
        _log("\nОстановка: нет whisper-cli. Установите whisper.cpp и повторите.")
        return 2

    # набор моделей
    models = DEFAULT_MODELS
    if args.models:
        models = {k: v for k, v in DEFAULT_MODELS.items() if k in set(args.models)}
        if not models:
            _log(f"  ! Неизвестные модели: {args.models}. Доступны: {list(DEFAULT_MODELS)}")
            return 2

    available = ensure_models(models, args.models_dir)
    if not available:
        return 2

    # тест-аудио
    if args.wav and args.wav.exists():
        wav = args.wav
        audio_seconds = _wav_seconds(wav)
        _log(f"\n== 4. Тест-аудио: {wav} ({audio_seconds:.1f}с, реальная запись) ==")
    else:
        wav = args.out_dir / "synth10s.wav"
        make_synthetic_wav(wav)
        audio_seconds = float(TEST_AUDIO_SECONDS)
        _log(f"\n== 4. Тест-аудио: синтетика {wav} ({audio_seconds:.0f}с) ==")
        _log("  (замер СКОРОСТИ; WER на синтетике не мерят — для качества дайте --wav речь)")

    # замеры
    _log(f"\n== 5. Замер моделей (threads={args.threads}) ==")
    results: list[BenchResult] = []
    for key, path in available.items():
        _log(f"  {key} ...")
        r = bench_model(
            key, path, wav, p.whisper_cli, args.threads,
            audio_seconds, p.has_usr_time,
        )
        results.append(r)

    # таблица-вердикт
    _log("\n== ИТОГ ==")
    _log(f"  {'модель':<24} {'RTF':>6} {'время,с':>9} {'RSS,МБ':>8}  вердикт")
    _log("  " + "-" * 74)
    for r in results:
        if r.ok:
            _log(f"  {r.model:<24} {r.rtf:>6.2f} {r.proc_seconds:>9.2f} "
                 f"{r.rss_mb:>8}  {verdict(r.rtf)}")
        else:
            _log(f"  {r.model:<24} {'—':>6} {'—':>9} {'—':>8}  ❌ {r.note}")

    _log("\n  RTF = длительность аудио / время обработки. Для 2 потоков через")
    _log(f"  один экземпляр: >= {RTF_COMFORTABLE} комфортно, {RTF_TIGHT}-{RTF_COMFORTABLE} впритык, "
         f"< {RTF_TIGHT} не тянет.")
    _log("  Замер на синтетике = скорость. Точность (WER) проверяйте отдельно на речи.")

    if not args.skip_capture:
        check_dual_capture(p, args.out_dir)

    _log("\nГотово. Вердикт по моделям выше — выбирайте по факту RTF/RSS, не по карточкам.")
    return 0


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return float(TEST_AUDIO_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
