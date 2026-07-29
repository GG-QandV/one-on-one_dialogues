"""Критерий 14 (§21): memory pressure — каскад деградирует ДО того, как
память доходит до MemoryMax, и не превышает его аварийно.

Подменённый MemoryReader (H2, карта критериев) + фиктивные Actions,
фиксирующие вызовы каскада (F1). Порог MEMORY_HARD — max_mb - hard_guard_mb,
т.е. каскад обязан среагировать С ЗАПАСОМ до реального предела.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.watchdog.degradation import DegradationCascade, DegradeConfig, Level
from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult


class _FakeReader:
    def __init__(self, mb: float) -> None:
        self.mb = mb

    def current_mb(self) -> float:
        return self.mb


@dataclass
class _FakeActions:
    calls: list[str] = field(default_factory=list)

    async def shorten_segments(self, enable: bool) -> None:
        self.calls.append(f"shorten_segments({enable})")

    async def pause_cloud_jobs(self, pause: bool) -> None:
        self.calls.append(f"pause_cloud_jobs({pause})")

    async def drop_caches(self) -> None:
        self.calls.append("drop_caches")

    async def stop_stt(self) -> None:
        self.calls.append("stop_stt")

    async def resume_stt(self) -> None:
        self.calls.append("resume_stt")

    async def force_tiny_model(self, enable: bool) -> None:
        self.calls.append(f"force_tiny_model({enable})")


async def _run(_env: CheckEnv) -> CheckResult:
    cfg = DegradeConfig()
    hard_threshold = cfg.max_mb - cfg.hard_guard_mb

    soft_actions = _FakeActions()
    soft_cascade = DegradationCascade(
        soft_actions, cfg, memory_reader=_FakeReader(cfg.high_mb + 10)
    )
    soft_level = await soft_cascade.tick()
    if soft_level is not Level.MEMORY_SOFT:
        return CheckResult(
            14,
            "memory pressure: каскад деградирует с запасом",
            CheckKind.AUTO,
            False,
            f"{cfg.high_mb + 10}МБ дал уровень {soft_level.name}, ожидался MEMORY_SOFT",
        )
    if "pause_cloud_jobs(True)" not in soft_actions.calls:
        return CheckResult(
            14,
            "memory pressure: каскад деградирует с запасом",
            CheckKind.AUTO,
            False,
            f"MEMORY_SOFT не поставил облачные задачи на паузу: {soft_actions.calls}",
        )

    mb_below_max = hard_threshold + 10  # строго между hard_threshold и max_mb
    if mb_below_max >= cfg.max_mb:
        return CheckResult(
            14,
            "memory pressure: каскад деградирует с запасом",
            CheckKind.AUTO,
            False,
            "hard_guard_mb настроен так, что тестовое значение достигает max_mb — проверьте конфиг",
        )

    hard_actions = _FakeActions()
    hard_cascade = DegradationCascade(hard_actions, cfg, memory_reader=_FakeReader(mb_below_max))
    hard_level = await hard_cascade.tick()
    if hard_level is not Level.MEMORY_HARD:
        return CheckResult(
            14,
            "memory pressure: каскад деградирует с запасом",
            CheckKind.AUTO,
            False,
            f"{mb_below_max}МБ (< max_mb={cfg.max_mb}) дал уровень {hard_level.name}, "
            "ожидался MEMORY_HARD",
        )
    if "stop_stt" not in hard_actions.calls:
        return CheckResult(
            14,
            "memory pressure: каскад деградирует с запасом",
            CheckKind.AUTO,
            False,
            f"MEMORY_HARD не остановил STT: {hard_actions.calls}",
        )

    margin = cfg.max_mb - mb_below_max
    return CheckResult(
        14,
        "memory pressure: каскад деградирует с запасом",
        CheckKind.AUTO,
        True,
        f"MEMORY_SOFT на {cfg.high_mb + 10}МБ → pause_cloud_jobs; "
        f"MEMORY_HARD на {mb_below_max}МБ → stop_stt, "
        f"запас до MemoryMax={cfg.max_mb}МБ: {margin}МБ",
    )


CHECK = CheckDef(
    number=14, title="memory pressure: деградация до MemoryMax", kind=CheckKind.AUTO, run=_run
)
