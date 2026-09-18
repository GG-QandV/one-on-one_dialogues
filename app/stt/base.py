"""app/stt/base.py — базовые интерфейсы STT-провайдеров (паттерн D1).

STT приводится к той же дисциплине, что и `provider.translation`:
именованный провайдер, единый `transcribe`, ключ через `KeyStore`,
проверка приватности до облачного вызова, общая классификация HTTP-ошибок.

`local_whisper` — офлайн, ему fence не нужен; `cloud_api` работает под
`Capability.AUDIO_TO_CLOUD` и обязан проверить профиль до отправки аудио.
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

from app.errors import (
    PrivacyViolation,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
    ProviderResponseInvalid,
    ProviderUnavailable,
)
from app.privacy import Capability, Fence, PrivacyController


@dataclass(frozen=True, slots=True)
class SttRequest:
    audio_path: Path
    segment_id: str
    #: `None` = автоопределение. Для локального whisper превращается в "auto".
    language_hint: Optional[str] = None
    #: Длительность аудио, мс — нужна для таймаута whisper.cpp.
    audio_ms: int = 0


@dataclass(frozen=True, slots=True)
class SttResult:
    raw_text: str
    detected_language: Optional[str] = None
    confidence: Optional[float] = None
    provider_request_id: Optional[str] = None
    #: Имя использованной модели — пишется в segments.stt_model.
    model: Optional[str] = None
    #: Имя звена, фактически распознавшего сегмент (для stt_provider_used).
    provider: Optional[str] = None


@runtime_checkable
class SttProvider(Protocol):
    """Протокол поставщика распознавания речи."""

    name: str

    async def transcribe(
        self, req: SttRequest, *, fence: Optional[Fence] = None
    ) -> SttResult:
        """Распознать один сегмент.

        `fence` обязателен для облачных провайдеров: захватывается в момент
        постановки задачи и предъявляется после сетевого вызова. Локальный
        провайдер его игнорирует.
        """
        ...

    async def close(self) -> None:
        """Освободить ресурсы."""
        ...

    def snapshot(self) -> dict:
        """Наблюдаемость для диагностического экрана (E5)."""
        ...


class BaseSttProvider(abc.ABC):
    """Общая обвязка облачных STT-провайдеров (зеркало `BaseTranslationProvider`).

    Наследники реализуют `_call` (HTTP) и `_parse` (разбор ответа). Здесь —
    приватностный гейт, ключ, таймаут и классификация ошибок: один код на
    все облачные провайдеры, без дублирования в третий раз.
    """

    def __init__(
        self,
        name: str,
        privacy: "PrivacyController",
        key_provider: Callable[[], str],
        *,
        timeout_s: float = 15.0,
    ) -> None:
        self._name = name
        self._privacy = privacy
        self._key_provider = key_provider
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        return self._name

    @property
    def privacy(self) -> "PrivacyController":
        return self._privacy

    @staticmethod
    def _classify(status_code: int, body: str = "") -> "ProviderError":
        """HTTP-код → исключение провайдера. Таблица D1 п.4."""
        _ = body
        if status_code in (401, 403):
            return ProviderAuthError(f"auth failed: {status_code}")
        if status_code == 429:
            return ProviderRateLimited(f"rate limited: {status_code}")
        if 500 <= status_code < 600:
            return ProviderUnavailable(f"server error: {status_code}")
        if status_code in (400, 422):
            return ProviderResponseInvalid(f"invalid response: {status_code}")
        return ProviderError(f"provider error: {status_code}")

    async def transcribe(
        self, req: SttRequest, *, fence: Optional[Fence] = None
    ) -> SttResult:
        capability = Capability.AUDIO_TO_CLOUD
        # 1. Приватность: аудио нельзя отправлять в облако в закрытом профиле.
        if self._privacy is not None:
            if not self._privacy.allows(capability):
                raise PrivacyViolation(
                    capability.value, self._privacy.profile.value
                )
            if fence is None:
                fence = self._privacy.fence()

        # 2. Ключ: отсутствие — явная ошибка до сети, а не молчаливый 401.
        api_key = self._key_provider() if self._key_provider is not None else ""
        if not api_key:
            raise ProviderAuthError("empty API key")

        # 3. Сетевой вызов с таймаутом. В тексте ошибки — только таймаут,
        # без пути к аудио (приватность данных).
        try:
            raw = await asyncio.wait_for(
                self._call(req, api_key), timeout=self._timeout_s
            )
        except TimeoutError:
            raise ProviderUnavailable(f"timeout {self._timeout_s}s") from None

        # 4. Валидация fence ПОСЛЕ вызова, ДО использования результата.
        if self._privacy is not None and fence is not None:
            self._privacy.validate(fence, capability)

        return self._parse(req, raw)

    @abc.abstractmethod
    async def _call(self, req: SttRequest, api_key: str) -> str:
        """HTTP-запрос к провайдеру, вернуть сырой ответ."""
        raise NotImplementedError

    @abc.abstractmethod
    def _parse(self, req: SttRequest, raw: str) -> SttResult:
        """Разобрать сырой ответ провайдера."""
        raise NotImplementedError

    async def close(self) -> None:
        return None
