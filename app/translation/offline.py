"""app/translation/offline.py — D7 offline gate and catch-up."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, Optional

from app.db import Database
from app.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderResponseInvalid,
)
from app.queue import JobQueue, JobType


class ProviderState(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class OfflineConfig:
    failures_to_degrade: int = 3
    probe_initial_s: float = 5.0
    probe_max_s: float = 300.0
    probe_factor: float = 2.0
    catch_up_batch: int = 200


class OfflineGate:
    def __init__(
        self,
        config: OfflineConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config or OfflineConfig()
        self._clock = clock
        # per-provider state
        self._state: dict[str, _Entry] = {}

    def _entry(self, provider: str) -> _Entry:
        if provider not in self._state:
            self._state[provider] = _Entry()
        return self._state[provider]

    def mark_unavailable(self, provider: str, err: ProviderError) -> None:
        entry = self._entry(provider)
        # Auth error -> immediate BLOCKED
        if isinstance(err, ProviderAuthError):
            entry.state = ProviderState.BLOCKED
            entry.last_error_code = "auth_error"
            return
        # Response invalid does not affect availability
        if isinstance(err, ProviderResponseInvalid):
            return
        # Retryable errors
        if isinstance(err, (ProviderUnavailable, ProviderRateLimited)):
            if entry.state == ProviderState.BLOCKED:
                # Should not happen, but ignore
                return
            if entry.state == ProviderState.DEGRADED:
                # This is a failed probe
                entry.probe_index += 1
                delay = self._probe_delay(entry.probe_index)
                entry.next_probe_at = self._clock() + delay
                # Keep last_error_code
                entry.last_error_code = err.__class__.__name__
                # consecutive_failures stays as is (already >= failures_to_degrade)
            else:  # AVAILABLE
                entry.consecutive_failures += 1
                entry.last_error_code = err.__class__.__name__
                if entry.consecutive_failures >= self._config.failures_to_degrade:
                    if entry.state != ProviderState.BLOCKED:
                        entry.state = ProviderState.DEGRADED
                        entry.probe_index = 0
                        # First probe delay
                        entry.next_probe_at = self._clock() + self._probe_delay(0)
        else:
            # Non-retryable error (e.g., bad request) does not affect availability
            # Just update last_error_code for diagnostics
            entry.last_error_code = err.__class__.__name__

    def mark_available(self, provider: str) -> None:
        entry = self._entry(provider)
        entry.state = ProviderState.AVAILABLE
        entry.consecutive_failures = 0
        entry.probe_index = 0
        entry.next_probe_at = 0.0
        entry.last_success_at = self._clock()
        entry.last_error_code = None

    def is_degraded(self, provider: str | None = None) -> bool:
        if provider is None:
            # global degraded: true if all known providers are degraded
            if not self._state:
                return False
            return all(
                entry.state == ProviderState.DEGRADED for entry in self._state.values()
            )
        else:
            entry = self._entry(provider)
            return entry.state == ProviderState.DEGRADED

    def should_attempt(self, provider: str) -> bool:
        entry = self._entry(provider)
        if entry.state == ProviderState.AVAILABLE:
            return True
        if entry.state == ProviderState.BLOCKED:
            return False
        # DEGRADED
        now = self._clock()
        return now >= entry.next_probe_at

    async def catch_up(self, db: Database, queue: JobQueue) -> int:
        # Select accurate segments with pending/failed translation, ordered by t_start_ms
        rows = await db.fetch_all(
            """
            SELECT id FROM segments
            WHERE track = 'accurate'
              AND translation_status IN ('pending', 'failed')
            ORDER BY t_start_ms ASC
            LIMIT ?
            """,
            (self._config.catch_up_batch,),
        )
        count = 0
        for row in rows:
            segment_id = row["id"]
            # idempotency key: segment_id
            await queue.enqueue(
                JobType.TRANSLATE,
                {"segment_id": segment_id},
                idempotency_key=segment_id,
            )
            count += 1
        return count

    def _probe_delay(self, probe_index: int) -> float:
        delay = (
            self._config.probe_initial_s
            * (self._config.probe_factor ** probe_index)
        )
        return min(delay, self._config.probe_max_s)

    def snapshot(self) -> dict:
        result = {}
        for provider, entry in self._state.items():
            result[provider] = {
                "state": entry.state.value,
                "consecutive_failures": entry.consecutive_failures,
                "probe_index": getattr(entry, "probe_index", 0),
                "next_probe_at": getattr(entry, "next_probe_at", 0.0),
                "last_error_code": entry.last_error_code,
                "last_success_at": getattr(entry, "last_success_at", None),
                "jobs_sent": getattr(entry, "jobs_sent", 0),
            }
        return result


@dataclass
class _Entry:
    state: ProviderState = ProviderState.AVAILABLE
    consecutive_failures: int = 0
    probe_index: int = 0
    next_probe_at: float = 0.0
    last_error_code: str | None = None
    last_success_at: float | None = None
    jobs_sent: int = 0