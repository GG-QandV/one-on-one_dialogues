"""app/stt/local_whisper.py — локальный провайдер whisper.cpp.

Обёртка над существующим `WhisperRunner` + `ModelSelector` + `parser`:
поведение не меняется, меняется только точка входа — теперь сегмент
приходит через `SttProvider.transcribe`, а не напрямую из scheduler.
Fallback base→tiny остаётся внутренним делом провайдера.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from app.privacy import Fence
from app.stt.base import SttRequest, SttResult
from app.stt.fallback import FallbackConfig, ModelSelector
from app.stt.parser import SttOutputMalformed, parse_whisper_result
from app.stt.runner import WhisperConfig, WhisperRunner

log = logging.getLogger(__name__)


class LocalWhisperProvider:
    """Офлайн-распознавание через whisper-cli. Один экземпляр на процесс."""

    name = "local_whisper"

    def __init__(
        self,
        *,
        model_path: Path,
        fallback_model_path: Path,
        binary: Path = Path("whisper-cli"),
        threads: int = 4,
        device: str = "auto",
        fallback_config: FallbackConfig | None = None,
    ) -> None:
        self._model_path = model_path
        self.device = device
        # device пока информационный: whisper.cpp на CPU; поле заведено
        # провайдерной секцией и будет задействовано при CUDA-сборке.
        self._runner = WhisperRunner(
            WhisperConfig(
                binary=binary,
                model_path=model_path,
                fallback_model_path=fallback_model_path,
                threads=threads,
            )
        )
        self._selector = ModelSelector(
            model_path, fallback_model_path, fallback_config
        )
        self._last_rtf: float | None = None

    async def transcribe(
        self, req: SttRequest, *, fence: Optional[Fence] = None
    ) -> SttResult:
        # fence не используется: распознавание локальное, за пределы процесса
        # аудио не уходит (Capability.LOCAL_STT).
        _ = fence
        raw = await self._runner.transcribe(
            req.audio_path,
            req.audio_ms,
            model_path=self._selector.current_path,
            language=req.language_hint or "auto",
        )
        self._last_rtf = raw.realtime_factor
        # Наблюдение питает fallback: следующий вызов может пойти на tiny.
        self._selector.observe(raw.realtime_factor)

        try:
            transcript = parse_whisper_result(raw.payload)
        except SttOutputMalformed:
            # Пустой/неполный ответ — не роняем сегмент: raw_text остаётся
            # пустым, дальше он просто не пойдёт в перевод (как раньше).
            log.warning("whisper вернул неполный JSON для %s", req.segment_id)
            return SttResult(raw_text="", model=raw.model_used)

        return SttResult(
            raw_text=transcript.text,
            detected_language=transcript.detected_language,
            confidence=transcript.confidence,
            model=raw.model_used,
        )

    async def close(self) -> None:
        # Runner не держит соединений — закрывать нечего.
        return None

    def snapshot(self) -> dict[str, Any]:
        snap = self._selector.snapshot()
        snap["provider"] = self.name
        snap["model_path"] = self._selector.current_path.name
        snap["device"] = self.device
        snap["last_rtf"] = round(self._last_rtf, 2) if self._last_rtf else None
        return snap
