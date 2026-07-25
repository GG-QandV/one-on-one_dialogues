"""app/stt/runner.py — запуск whisper.cpp на один сегмент. Задача C4a.

Спека: раздел 7 — режим файл-на-сегмент, `whisper-cli` с JSON-выводом.

Контракт с процессом
--------------------
Один вызов = один WAV = один JSON. Никакого stdin-стриминга: спека закрыла
парадигму streaming в пользу файл-на-сегмент (закрытие риска R3), потому что
только JSON-вывод отдаёт таймкоды и логпробы токенов для stt_confidence.

Управление процессом
--------------------
  * жёсткий таймаут: зависший whisper убивается вместе с группой процессов,
    иначе scheduler встанет навсегда (а он один на оба потока);
  * никакого shell=True: аргументы передаются списком;
  * stdout не читается — whisper пишет результат в файл `<wav>.json`,
    stderr дренируется в лог с ограничением объёма.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.errors import ModelNotAvailable, SttError, SttOutputMalformed

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WhisperConfig:
    """Секция [stt] config.toml."""

    binary: Path = Path("whisper-cli")
    model_path: Path = Path("models/ggml-base.bin")
    fallback_model_path: Path = Path("models/ggml-tiny.bin")
    threads: int = 4
    #: Язык 'auto' включает автодетект (спека, раздел 4).
    language: str = "auto"
    #: Таймаут как множитель длительности аудио + константа. Сегмент 5 с
    #: при live-пригодном железе обрабатывается за ~1 с; таймаут 5*2+10=20 с
    #: срабатывает только на действительно зависшем процессе.
    timeout_factor: float = 2.0
    timeout_base_s: float = 10.0
    beam_size: int = 0            # 0 = greedy; beam недопустим по бюджету CPU
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WhisperRawResult:
    """Сырой результат одного вызова. Разбор смысла — в parser.py (C6)."""

    json_path: Path
    payload: dict[str, Any]
    model_used: str
    wall_ms: int
    audio_ms: int

    @property
    def realtime_factor(self) -> float:
        """< 1.0 — быстрее реального времени. Питает деградацию (F2)."""
        return self.wall_ms / max(1, self.audio_ms)


class WhisperRunner:
    """Исполнитель одного вызова. Не знает про очереди и потоки —
    сериализацию доступа обеспечивает scheduler (C4b)."""

    def __init__(self, config: WhisperConfig) -> None:
        self._cfg = config
        self._verify()

    def _verify(self) -> None:
        if not self._cfg.model_path.exists():
            raise ModelNotAvailable(f"модель не найдена: {self._cfg.model_path}")

    async def transcribe(
        self,
        wav_path: Path,
        audio_ms: int,
        *,
        model_path: Path | None = None,
        language: str | None = None,
    ) -> WhisperRawResult:
        model = model_path or self._cfg.model_path
        if not model.exists():
            raise ModelNotAvailable(f"модель не найдена: {model}")
        if not wav_path.exists():
            raise SttError(f"WAV не найден: {wav_path}")

        out_prefix = wav_path.with_suffix("")  # whisper добавит .json сам
        lang = language or self._cfg.language
        cmd = [
            str(self._cfg.binary),
            "-m", str(model),
            "-f", str(wav_path),
            "-l", lang,
            "-t", str(self._cfg.threads),
            "-oj",                      # JSON-вывод — источник таймкодов и логпроб
            "-of", str(out_prefix),
            "-np",                      # без прогресс-баров в stderr
            *self._cfg.extra_args,
        ]

        timeout = self._cfg.timeout_base_s + audio_ms / 1000 * self._cfg.timeout_factor
        started = time.perf_counter()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            # Своя группа: убийство зависшего whisper не оставит внуков.
            preexec_fn=os.setsid,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            self._kill_group(proc)
            await proc.wait()
            raise SttError(
                f"whisper превысил таймаут {timeout:.0f} с на {wav_path.name}"
            ) from exc

        wall_ms = int((time.perf_counter() - started) * 1000)

        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace")[-300:]
            raise SttError(
                f"whisper завершился с кодом {proc.returncode}: {detail}"
            )

        json_path = out_prefix.with_suffix(".json")
        if not json_path.exists():
            raise SttOutputMalformed(f"whisper не создал {json_path.name}")
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SttOutputMalformed(f"невалидный JSON в {json_path.name}: {exc}") from exc

        result = WhisperRawResult(
            json_path=json_path,
            payload=payload,
            model_used=model.name,
            wall_ms=wall_ms,
            audio_ms=audio_ms,
        )
        log.info(
            "whisper %s: %d мс аудио за %d мс (x%.2f, %s)",
            wav_path.name, audio_ms, wall_ms, result.realtime_factor, model.name,
        )
        return result

    @staticmethod
    def _kill_group(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
