"""app/delivery/hotkey.py — G3 hotkey listener for clipboard delivery."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HotkeyConfig:
    combination: str = "ctrl+alt+c"
    cooldown_s: float = 1.0


CopyFn = Callable[[str], "Awaitable[bool]"]


class HotkeyListener:
    """Listen for hotkey combination and trigger clipboard copy.

    In MVP — stub that logs activity. Platform-specific hotkey binding
    (evdev/keyboard on Linux) is tier 2; the configuration key is accepted
    but the hotkey registration is a log message until tier 2.
    """

    def __init__(
        self,
        config: HotkeyConfig | None = None,
        *,
        get_latest_draft: Callable[[], "str | None"],
        copy_fn: CopyFn,
        clock: Callable[[], float] = __import__("time").monotonic,
    ) -> None:
        self._cfg = config or HotkeyConfig()
        self._get_latest_draft = get_latest_draft
        self._copy_fn = copy_fn
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_trigger: float = 0.0
        self._stats: dict[str, int] = {"triggered": 0, "copied": 0, "nothing": 0}

    async def start(self) -> None:
        if self._task is not None:
            return
        log.info(
            "горячая клавиша %s (в MVP — мониторинг; платформенный хук — тир 2)",
            self._cfg.combination,
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="hotkey")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        """In MVP: periodic check stub (tier 2 will use evdev listener)."""
        while not self._stop.is_set():
            await asyncio.sleep(30.0)

    def trigger(self) -> None:
        """Called by platform-specific hook (tier 2) or manually."""
        now = self._clock()
        if now - self._last_trigger < self._cfg.cooldown_s:
            return
        self._last_trigger = now
        self._stats["triggered"] += 1
        text = self._get_latest_draft()
        if not text:
            self._stats["nothing"] += 1
            return
        asyncio.create_task(self._do_copy(text))

    async def _do_copy(self, text: str) -> None:
        ok = await self._copy_fn(text)
        if ok:
            self._stats["copied"] += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "hotkey": self._cfg.combination,
            "listening": self._task is not None and not self._stop.is_set(),
            "stats": dict(self._stats),
        }
