"""app/stt/chain.py — цепочка фолбэков STT.

Отличия от `OfflineGate` (D7) намеренные и зафиксированы спекой:
для перевода пауза в десятки минут уместна, для STT — нет. Поэтому здесь
не DEGRADED/BLOCKED с фоновыми пробами, а лёгкий **cooldown на звено**:
упавший провайдер пропускается `cooldown_s`, а следующая попытка — это
следующий пришедший сегмент. Своего таймера у цепочки нет.

Инвариант §8.7: последнее звено (`local_whisper`) не имеет cooldown и
обязано либо распознать сегмент, либо поднять ошибку выше.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from app.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseInvalid,
    ProviderUnavailable,
    SpeechLocalError,
)
from app.privacy import Fence
from app.stt.base import SttProvider, SttRequest, SttResult

log = logging.getLogger(__name__)


class SttChainExhausted(SpeechLocalError):  # noqa: N818 — имя зафиксировано спекой
    """Ни одно звено не дало результат. Сессию не останавливает."""

    code = "STT_CHAIN_EXHAUSTED"
    retryable = False


#: Ошибки, переводящие звено на cooldown. NotImplementedError — облачный
#: провайдер ещё не реализован (D2/D3): трактуем как недоступность, чтобы
#: цепочка доходила до local_whisper, а не падала на первом облачном звене.
_COOLDOWN_ERRORS = (
    ProviderUnavailable,
    ProviderRateLimited,
    ProviderAuthError,
    NotImplementedError,
)


@dataclass(slots=True)
class ChainEntry:
    provider: SttProvider
    cooldown_s: float
    blocked_until: float = 0.0
    #: Звено уже в полёте (защита от дублей при hedging).
    inflight: bool = False


class SttFailoverChain:
    """Протокол `SttProvider`: пробует звенья по порядку."""

    name = "failover_chain"

    def __init__(
        self,
        entries: Sequence[ChainEntry],
        *,
        clock: Callable[[], float] = time.monotonic,
        hedge_delay_s: float | None = None,
        deadline_s: float | None = None,
    ) -> None:
        if not entries:
            raise ValueError("chain must not be empty")
        self._entries = list(entries)
        self._clock = clock
        #: None — строго последовательный обход (local/тесты).
        #: Число — hedging: старт следующего звена, если предыдущее молчит.
        self._hedge_delay_s = hedge_delay_s
        self._deadline_s = deadline_s

    async def transcribe(
        self, req: SttRequest, *, fence: Optional[Fence] = None
    ) -> SttResult:
        if self._hedge_delay_s is None:
            return await self._sequential(req, fence=fence)
        return await self._hedged(req, fence=fence)

    def _finish(self, entry: ChainEntry, result: SttResult) -> SttResult:
        return dataclasses.replace(
            result,
            provider=entry.provider.name,
            entry=getattr(entry.provider, "label", entry.provider.name),
        )

    async def _attempt(
        self, entry: ChainEntry, req: SttRequest, fence: Optional[Fence]
    ) -> SttResult:
        entry.inflight = True
        try:
            return await entry.provider.transcribe(req, fence=fence)
        finally:
            entry.inflight = False

    async def _sequential(
        self, req: SttRequest, *, fence: Optional[Fence] = None
    ) -> SttResult:
        now = self._clock()
        last_exc: BaseException | None = None
        for entry in self._entries:
            if entry.blocked_until > now:
                continue  # звено на cooldown — пропускаем без попытки
            try:
                result = await entry.provider.transcribe(req, fence=fence)
                return self._finish(entry, result)
            except _COOLDOWN_ERRORS as exc:
                last_exc = exc
                if entry.cooldown_s > 0:
                    entry.blocked_until = now + entry.cooldown_s
                log.warning(
                    "STT звено %s недоступно: %s", entry.provider.name, exc
                )
                continue
            except ProviderResponseInvalid as exc:
                # Битый ответ — проблема сегмента, не доступности провайдера:
                # cooldown не ставим, но для этого сегмента идём дальше.
                last_exc = exc
                continue
        raise SttChainExhausted("все звенья STT недоступны") from last_exc

    async def _hedged(
        self, req: SttRequest, *, fence: Optional[Fence] = None
    ) -> SttResult:
        """Hedging облачных звеньев: следующее стартует, если предыдущее
        молчит `hedge_delay_s`; первый успех выигрывает, прочие отменяются.
        Общий потолок — `deadline_s`."""
        hedge = self._hedge_delay_s or 0.0
        deadline = self._clock() + (self._deadline_s or 30.0)
        pending = [
            e for e in self._entries
            if e.blocked_until <= self._clock() and not e.inflight
        ]
        if not pending:
            raise SttChainExhausted("все звенья STT на cooldown")

        start = self._clock()
        launched = 0
        running: dict[asyncio.Task, ChainEntry] = {}
        last_exc: BaseException | None = None
        winner: tuple[ChainEntry, SttResult] | None = None
        try:
            while pending or running:
                now = self._clock()
                if now >= deadline:
                    break
                # Пора запускать следующее звено?
                if pending and (
                    not running or now - start >= launched * hedge
                ):
                    entry = pending.pop(0)
                    if entry.inflight:
                        continue
                    running[asyncio.create_task(self._attempt(entry, req, fence))] = entry
                    launched += 1
                    continue

                if not running:
                    break
                wait_s = deadline - now
                if pending:
                    wait_s = min(wait_s, max(0.001, (start + launched * hedge) - now))
                done, _ = await asyncio.wait(
                    list(running), timeout=wait_s, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    continue  # тик до старта следующего звена или до дедлайна
                for task in done:
                    entry = running.pop(task)
                    try:
                        winner = (entry, task.result())
                        break
                    except _COOLDOWN_ERRORS as exc:
                        last_exc = exc
                        if entry.cooldown_s > 0:
                            entry.blocked_until = self._clock() + entry.cooldown_s
                        log.warning(
                            "STT звено %s недоступно: %s", entry.provider.name, exc
                        )
                    except ProviderResponseInvalid as exc:
                        last_exc = exc
                if winner is not None:
                    break
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

        if winner is None:
            raise SttChainExhausted("все звенья STT недоступны") from last_exc
        return self._finish(winner[0], winner[1])

    async def close(self) -> None:
        for entry in self._entries:
            try:
                await entry.provider.close()
            except Exception:  # noqa: BLE001 — закрытие не должно мешать
                log.exception("ошибка закрытия STT-звена %s", entry.provider.name)

    def snapshot(self) -> dict:
        now = self._clock()
        return {
            "provider": self.name,
            "last_rtf": None,
            "entries": [
                {
                    "provider": e.provider.name,
                    "entry": getattr(e.provider, "label", e.provider.name),
                    "cooldown_s": e.cooldown_s,
                    "blocked": e.blocked_until > now,
                }
                for e in self._entries
            ],
        }
