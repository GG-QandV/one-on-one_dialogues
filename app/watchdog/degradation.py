"""app/watchdog/degradation.py — каскад деградации. Задачи F2a, F2b.

Спека: раздел 15 «Каскад деградации», приоритет «сохранность данных выше
скорости live». Пороги — из config.toml, секция [memory] и [latency].

Устройство
----------
Каскад — конечный автомат уровней. Уровень определяется худшим из активных
триггеров; действия применяются при переходах, а не на каждом тике:
повторное применение того же уровня — no-op.

    L0 NORMAL      всё в штатном режиме
    L1 LATENCY     задержка точного трека > порога ИЛИ backlog STT
    L2 MEMORY_SOFT RAM > high_mb: стоп новых облачных задач, сброс кэшей
    L3 MEMORY_HARD RAM > max_mb - guard: корректный стоп STT, только запись

Гистерезис: возврат на уровень ниже требует, чтобы метрика ушла ниже порога
возврата (ниже порога входа) и продержалась там min_hold тиков. Без этого
каскад дребезжит на границе порога, а каждое переключение — это реальные
действия (пауза очередей, смена модели).

Действия выражены интерфейсом Actions: каскад не знает про scheduler,
jobs и whisper напрямую — main отдаёт ему связку колбэков. Это позволяет
smoke-тестировать автомат целиком без единого настоящего компонента.

Чтение памяти
-------------
cgroup v2: /sys/fs/cgroup/<путь юнита>/memory.current. Путь берётся из
/proc/self/cgroup. Вне systemd (разработка) — фолбэк на /proc/self/status
(VmRSS): менее точен (не считает дочерний whisper), о чём честно пишется
в снапшот.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger(__name__)


class Level(IntEnum):
    NORMAL = 0
    LATENCY = 1
    MEMORY_SOFT = 2
    MEMORY_HARD = 3


@dataclass(frozen=True, slots=True)
class DegradeConfig:
    """[memory] и [latency] из config.toml."""

    high_mb: int = 1750
    max_mb: int = 1900
    #: Отступ от max: реагировать надо ДО того, как systemd пришлёт OOM.
    hard_guard_mb: int = 50
    latency_target_ms: int = 3000
    #: Медиана задержки по этому окну сегментов.
    latency_window: int = 10
    tick_s: float = 2.0
    #: Возврат вниз: метрика ниже (порог * restore_ratio) в течение min_hold.
    restore_ratio: float = 0.85
    min_hold_ticks: int = 3


class Actions(Protocol):
    """Что каскад умеет делать с системой. Реализует main."""

    async def shorten_segments(self, enable: bool) -> None:
        """L1: уменьшить порог паузы/макс длину, отключить частичные."""
        ...

    async def pause_cloud_jobs(self, pause: bool) -> None:
        """L2: не брать новые translate/draft задачи."""
        ...

    async def drop_caches(self) -> None:
        """L2: сброс необязательных кэшей (однократно на вход в уровень)."""
        ...

    async def stop_stt(self) -> None:
        """L3: корректно остановить STT, очередь сохранить (recording-only)."""
        ...

    async def resume_stt(self) -> None:
        """Выход из L3."""
        ...

    async def force_tiny_model(self, enable: bool) -> None:
        """L1 по CPU: принудительно tiny (поверх автофолбэка C7)."""
        ...


# ------------------------------------------------------------- чтение памяти

class MemoryReader:
    def __init__(self) -> None:
        self._cgroup_file = self._locate_cgroup()
        self.source = "cgroup" if self._cgroup_file else "vmrss"
        if not self._cgroup_file:
            log.warning(
                "cgroup memory.current недоступен — считаю по VmRSS "
                "(дочерний whisper НЕ учитывается, пороги занижены)"
            )

    @staticmethod
    def _locate_cgroup() -> Path | None:
        try:
            line = Path("/proc/self/cgroup").read_text().strip().splitlines()[0]
            # формат v2: "0::/user.slice/.../speech-gateway.service"
            rel = line.split("::", 1)[1]
            path = Path("/sys/fs/cgroup") / rel.lstrip("/") / "memory.current"
            return path if path.exists() else None
        except (OSError, IndexError):
            return None

    def current_mb(self) -> float:
        if self._cgroup_file is not None:
            try:
                return int(self._cgroup_file.read_text()) / (1 << 20)
            except (OSError, ValueError):
                pass
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
        except OSError:
            pass
        return 0.0


# ---------------------------------------------------------------- сам каскад

class DegradationCascade:
    def __init__(
        self,
        actions: Actions,
        config: DegradeConfig | None = None,
        *,
        memory_reader: MemoryReader | None = None,
        latency_source: Callable[[], list[int]] | None = None,
        backlog_source: Callable[[], bool] | None = None,
    ) -> None:
        self._a = actions
        self._cfg = config or DegradeConfig()
        self._mem = memory_reader or MemoryReader()
        #: Последние задержки точного трека, мс. Питает E5 и снапшот.
        self._latency_source = latency_source or (lambda: [])
        self._backlog_source = backlog_source or (lambda: False)

        self._level = Level.NORMAL
        self._hold = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._transitions = 0
        self._last_mem_mb = 0.0
        self._last_latency_ms: int | None = None

    @property
    def level(self) -> Level:
        return self._level

    # ------------------------------------------------------------------ цикл

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="degradation")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — watchdog не умирает
                log.exception("тик каскада упал")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.tick_s)

    async def tick(self) -> Level:
        """Один шаг автомата. Публичен ради smoke: время инжектируется тиками."""
        desired = self._evaluate()
        if desired > self._level:
            await self._transition(desired, upward=True)
            self._hold = 0
        elif desired < self._level:
            self._hold += 1
            if self._hold >= self._cfg.min_hold_ticks:
                await self._transition(desired, upward=False)
                self._hold = 0
        else:
            self._hold = 0
        return self._level

    # ------------------------------------------------------------ вычисление

    def _evaluate(self) -> Level:
        cfg = self._cfg
        mem = self._mem.current_mb()
        self._last_mem_mb = mem

        latencies = self._latency_source()[-cfg.latency_window:]
        median = sorted(latencies)[len(latencies) // 2] if latencies else None
        self._last_latency_ms = median

        # При спуске пороги умножаются на restore_ratio (гистерезис).
        down = self._level > Level.NORMAL
        k = cfg.restore_ratio if down else 1.0

        hard = cfg.max_mb - cfg.hard_guard_mb
        if mem >= (hard * k if self._level >= Level.MEMORY_HARD else hard):
            return Level.MEMORY_HARD
        if mem >= (cfg.high_mb * k if self._level >= Level.MEMORY_SOFT else cfg.high_mb):
            return Level.MEMORY_SOFT

        lat_thr = cfg.latency_target_ms * (k if self._level >= Level.LATENCY else 1.0)
        if (median is not None and median > lat_thr) or self._backlog_source():
            return Level.LATENCY
        return Level.NORMAL

    # -------------------------------------------------------------- переходы

    async def _transition(self, target: Level, *, upward: bool) -> None:
        log.warning(
            "деградация: %s -> %s (RAM %.0f МБ, медиана задержки %s мс)",
            self._level.name, target.name, self._last_mem_mb,
            self._last_latency_ms,
        )
        self._transitions += 1
        previous, self._level = self._level, target

        # Действия применяются по разности уровней: и на подъём, и на спуск.
        await self._apply(Level.LATENCY, previous, target,
                          on=lambda: self._enter_latency(),
                          off=lambda: self._exit_latency())
        await self._apply(Level.MEMORY_SOFT, previous, target,
                          on=lambda: self._enter_soft(),
                          off=lambda: self._exit_soft())
        await self._apply(Level.MEMORY_HARD, previous, target,
                          on=lambda: self._a.stop_stt(),
                          off=lambda: self._a.resume_stt())

    @staticmethod
    async def _apply(
        threshold: Level, prev: Level, cur: Level,
        on: Callable[[], Awaitable[None]], off: Callable[[], Awaitable[None]],
    ) -> None:
        if prev < threshold <= cur:
            await on()
        elif cur < threshold <= prev:
            await off()

    async def _enter_latency(self) -> None:
        await self._a.shorten_segments(True)
        await self._a.force_tiny_model(True)

    async def _exit_latency(self) -> None:
        await self._a.shorten_segments(False)
        await self._a.force_tiny_model(False)

    async def _enter_soft(self) -> None:
        await self._a.pause_cloud_jobs(True)
        await self._a.drop_caches()

    async def _exit_soft(self) -> None:
        await self._a.pause_cloud_jobs(False)

    # ------------------------------------------------------------ наблюдаемость

    def snapshot(self) -> dict[str, Any]:
        return {
            "level": self._level.name,
            "memory_mb": round(self._last_mem_mb, 1),
            "memory_source": self._mem.source,
            "latency_median_ms": self._last_latency_ms,
            "transitions": self._transitions,
            "hold": self._hold,
            "thresholds": {
                "high_mb": self._cfg.high_mb,
                "hard_mb": self._cfg.max_mb - self._cfg.hard_guard_mb,
                "latency_ms": self._cfg.latency_target_ms,
            },
        }
