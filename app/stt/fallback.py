"""app/stt/fallback.py — выбор модели по фактической скорости. Задача C7.

Спека: раздел 15 «CPU не успевает → переход base → tiny», раздел 7
«параллельный запуск второй модели запрещён».

Логика
------
Решение принимается по скользящему окну realtime factor (отношение времени
обработки к длительности аудио). Порог перехода вниз ниже порога возврата —
гистерезис, без него на границе нагрузки модель будет прыгать туда-обратно
на каждом сегменте, а каждая смена — это перезагрузка весов с диска.

Смена модели не мгновенна: применяется к следующему вызову, а не текущему.
Одновременно двух моделей в памяти нет — scheduler сериализует вызовы,
и переключение происходит между ними.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ModelTier(str, Enum):
    BASE = "base"
    TINY = "tiny"


@dataclass(frozen=True, slots=True)
class FallbackConfig:
    #: Переход на tiny, если медиана окна выше этого фактора.
    degrade_above_rtf: float = 0.9
    #: Возврат на base, если медиана ниже. Зазор — гистерезис.
    restore_below_rtf: float = 0.55
    window: int = 6
    #: Минимум наблюдений до первого решения.
    min_samples: int = 3
    #: После возврата на base не деградировать снова раньше N сегментов —
    #: защита от осцилляции при пилообразной нагрузке.
    cooldown_segments: int = 4


class ModelSelector:
    """Хранит текущий тир и решает о переключении. Потокобезопасность не
    нужна: вызывается только из scheduler, который однопоточен по построению."""

    def __init__(
        self,
        base_path: Path,
        tiny_path: Path,
        config: FallbackConfig | None = None,
    ) -> None:
        self._paths = {ModelTier.BASE: base_path, ModelTier.TINY: tiny_path}
        self._cfg = config or FallbackConfig()
        self._tier = ModelTier.BASE
        self._rtf: deque[float] = deque(maxlen=self._cfg.window)
        self._cooldown = 0
        self._switches = 0

    @property
    def tier(self) -> ModelTier:
        return self._tier

    @property
    def current_path(self) -> Path:
        return self._paths[self._tier]

    def observe(self, realtime_factor: float) -> ModelTier:
        """Учесть замер и вернуть тир для СЛЕДУЮЩЕГО вызова."""
        self._rtf.append(realtime_factor)
        if self._cooldown > 0:
            self._cooldown -= 1
        if len(self._rtf) < self._cfg.min_samples:
            return self._tier

        ordered = sorted(self._rtf)
        median = ordered[len(ordered) // 2]

        if (
            self._tier is ModelTier.BASE
            and median > self._cfg.degrade_above_rtf
            and self._cooldown == 0
        ):
            self._switch(ModelTier.TINY, median)
        elif self._tier is ModelTier.TINY and median < self._cfg.restore_below_rtf:
            self._switch(ModelTier.BASE, median)
            self._cooldown = self._cfg.cooldown_segments
        return self._tier

    def force(self, tier: ModelTier, reason: str) -> None:
        """Принудительное переключение от каскада деградации (F2)."""
        if tier is not self._tier:
            log.warning("модель принудительно: %s (%s)", tier.value, reason)
            self._tier = tier
            self._rtf.clear()

    def _switch(self, tier: ModelTier, median: float) -> None:
        log.warning(
            "смена модели %s -> %s (медиана rtf %.2f)",
            self._tier.value, tier.value, median,
        )
        self._tier = tier
        self._switches += 1
        # Окно очищается: замеры старой модели не описывают новую.
        self._rtf.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "tier": self._tier.value,
            "rtf_window": [round(x, 2) for x in self._rtf],
            "switches": self._switches,
            "cooldown": self._cooldown,
        }
