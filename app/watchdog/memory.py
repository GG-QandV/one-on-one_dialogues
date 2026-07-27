"""app/watchdog/memory.py — F1 memory monitor with polling, history, child sum.

Реализует протокол MemoryReader из degradation.py (source + current_mb),
чтобы подставляться в DegradationCascade без адаптера.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

log = logging.getLogger(__name__)

Source = Literal["cgroup", "vmrss"]


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    interval_s: float = 2.0
    history_size: int = 300
    high_mb: float = 1750.0
    max_mb: float = 1900.0


@dataclass(frozen=True, slots=True)
class MemorySample:
    at_ms: int
    total_mb: float
    self_mb: float
    children_mb: float
    source: Source


class MemoryMonitor:
    def __init__(
        self,
        config: MemoryConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = config or MemoryConfig()
        self._clock = clock
        self._source: Source
        self._cgroup_path: Path | None = None
        self._pid = _my_pid()
        self._warned_degraded = False

        # Состояние опроса
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_sample: MemorySample | None = None
        self._peak_mb = 0.0
        self._history: deque[MemorySample] = deque(maxlen=self._cfg.history_size)

        # Выбор источника при инициализации
        self._init_source()

    # --------------------------------------------------------------- source init

    def _init_source(self) -> None:
        cg = _locate_cgroup_v2()
        if cg is not None:
            self._source = "cgroup"
            self._cgroup_path = cg
            return
        cg1 = _locate_cgroup_v1()
        if cg1 is not None:
            self._source = "cgroup"
            self._cgroup_path = cg1
            return
        self._source = "vmrss"
        self._cgroup_path = None
        if not self._warned_degraded:
            log.warning(
                "cgroup memory.current недоступен — считаю по VmRSS "
                "(суммирую дочерние процессы; пороги могут быть занижены)"
            )
            self._warned_degraded = True

    # --------------------------------------------------------------- properties

    @property
    def source(self) -> Source:
        return self._source

    @property
    def degraded_source(self) -> bool:
        return self._source == "vmrss"

    # --------------------------------------------------------------- polling

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="memory-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._cfg.interval_s
                )

    def _tick(self) -> None:
        sample = self.read()
        self._history.append(sample)
        if sample.total_mb > self._peak_mb:
            self._peak_mb = sample.total_mb
        self._last_sample = sample

        # Лог на пересечении порогов
        if sample.total_mb >= self._cfg.max_mb:
            log.warning("память %.0f МБ >= max (%.0f)", sample.total_mb, self._cfg.max_mb)
        elif sample.total_mb >= self._cfg.high_mb:
            log.info("память %.0f МБ >= high (%.0f)", sample.total_mb, self._cfg.high_mb)

    # --------------------------------------------------------------- MemoryReader protocol

    def current_mb(self) -> float:
        """Последнее измеренное значение. Нет I/O — вызывается из тика каскада."""
        if self._last_sample is not None:
            return self._last_sample.total_mb
        return 0.0

    # --------------------------------------------------------------- one-shot

    def read(self) -> MemorySample:
        """Синхронное разовое измерение. Может делать I/O."""
        at_ms = int(self._clock() * 1000)
        self_mb = self._read_self_mb()
        children_mb = self._read_children_mb() if self._source == "vmrss" else 0.0
        total_mb = self_mb + children_mb
        return MemorySample(
            at_ms=at_ms,
            total_mb=round(total_mb, 1),
            self_mb=round(self_mb, 1),
            children_mb=round(children_mb, 1),
            source=self._source,
        )

    def _read_self_mb(self) -> float:
        if self._cgroup_path is not None:
            try:
                return int(self._cgroup_path.read_text()) / (1 << 20)
            except (OSError, ValueError):
                pass
        return _read_vmrss(self._pid)

    def _read_children_mb(self) -> float:
        total = 0.0
        for pid in _walk_children(self._pid):
            total += _read_vmrss(pid)
        return total

    # --------------------------------------------------------------- history & peak

    def history(self, n: int | None = None) -> tuple[MemorySample, ...]:
        if n is None:
            return tuple(self._history)
        return tuple(list(self._history)[-n:])

    def peak_mb(self) -> float:
        return self._peak_mb

    # --------------------------------------------------------------- snapshot

    def snapshot(self) -> dict[str, Any]:
        return {
            "source": self._source,
            "degraded_source": self.degraded_source,
            "memory_mb": round(self.current_mb(), 1),
            "self_mb": round(self._last_sample.self_mb, 1) if self._last_sample else 0.0,
            "children_mb": round(self._last_sample.children_mb, 1) if self._last_sample else 0.0,
            "peak_mb": round(self._peak_mb, 1),
            "history_size": len(self._history),
            "history_capacity": self._cfg.history_size,
            "high_mb": self._cfg.high_mb,
            "max_mb": self._cfg.max_mb,
            "available": self._source is not None,
        }


# ------------------------------------------------------------------ helpers


def _my_pid() -> int:
    try:
        return int(Path("/proc/self/stat").read_text().split()[0])
    except (OSError, IndexError, ValueError):
        return 0


def _read_vmrss(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _walk_children(ppid: int) -> list[int]:
    """Рекурсивный обход дочерних процессов по PPid в /proc."""
    result: list[int] = []
    seen: set[int] = set()
    frontier = [ppid]
    while frontier:
        parent = frontier.pop()
        for child in _find_children(parent):
            if child not in seen and child != parent:
                seen.add(child)
                result.append(child)
                frontier.append(child)
    return result


def _find_children(ppid: int) -> list[int]:
    """Найти PID процессов, чей PPid == заданный."""
    children: list[int] = []
    try:
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                status = (proc / "status").read_text()
                for line in status.splitlines():
                    if line.startswith("PPid:"):
                        if int(line.split()[1]) == ppid:
                            children.append(int(proc.name))
                        break
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass
    return children


def _locate_cgroup_v2() -> Path | None:
    try:
        lines = Path("/proc/self/cgroup").read_text().strip().splitlines()
        for line in lines:
            if "::" in line:
                rel = line.split("::", 1)[1]
                path = Path("/sys/fs/cgroup") / rel.lstrip("/") / "memory.current"
                if path.exists():
                    return path
    except OSError:
        pass
    return None


def _locate_cgroup_v1() -> Path | None:
    path = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    return path if path.exists() else None
