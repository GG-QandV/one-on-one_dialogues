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


class SttFailoverChain:
    """Протокол `SttProvider`: пробует звенья по порядку."""

    name = "failover_chain"

    def __init__(
        self,
        entries: Sequence[ChainEntry],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not entries:
            raise ValueError("chain must not be empty")
        if entries[-1].cooldown_s != 0.0:
            raise ValueError(
                "last entry (local_whisper) must have no cooldown (invariant §8.7)"
            )
        self._entries = list(entries)
        self._clock = clock

    async def transcribe(
        self, req: SttRequest, *, fence: Optional[Fence] = None
    ) -> SttResult:
        now = self._clock()
        last_exc: BaseException | None = None
        for entry in self._entries:
            if entry.blocked_until > now:
                continue  # звено на cooldown — пропускаем без попытки
            try:
                result = await entry.provider.transcribe(req, fence=fence)
                return dataclasses.replace(
                    result,
                    provider=entry.provider.name,
                    entry=getattr(entry.provider, "label", entry.provider.name),
                )
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
