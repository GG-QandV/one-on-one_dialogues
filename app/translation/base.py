"""app/translation/base.py — базовые интерфейсы провайдеров перевода. Задача D1 роадмапа."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Literal, Optional, Protocol, runtime_checkable

from app.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderResponseInvalid,
    ProviderError,
    PrivacyViolation,
)
from app.privacy import Capability, Fence, PrivacyController


# ------------------------------------------------------------------ enums


class TranslationMode(str, Enum):
    LIVE_LITERAL = "live_literal"
    LIVE_SAFE = "live_safe"
    POST_CLEAN = "post_clean"


# ------------------------------------------------------------------ data classes


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    text: str
    source_language: str
    target_language: str
    mode: TranslationMode
    context: tuple[str, ...] = ()
    segment_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class Change:
    type: Literal["filler_removed", "punctuation", "lost_entity", "other"]
    original: str
    replacement: str


@dataclass(frozen=True, slots=True)
class TranslationResult:
    translation_raw: str
    translation_clean: Optional[str] = None
    changes: tuple[Change, ...] = ()
    provider_request_id: Optional[str] = None


# ------------------------------------------------------------------ provider protocol


@runtime_checkable
class TranslationProvider(Protocol):
    """Протокол поставщика перевода."""

    name: str
    privacy: "PrivacyController"

    async def translate(self, req: "TranslationRequest", *, fence: "Fence") -> "TranslationResult":
        """Перевести текст."""
        ...

    async def close(self) -> None:
        """Освободить ресурсы."""
        ...


# ------------------------------------------------------------------ base class


class BaseTranslationProvider(abc.ABC):
    """Общая обвязка. Наследники реализуют _call и _parse."""

    Auditor = Callable[["TranslationRequest", "TranslationResult"], "TranslationResult"]

    def __init__(
        self,
        name: str,
        privacy: "PrivacyController",
        key_provider: Callable[[], str],
        *,
        timeout_s: float = 15.0,
        auditor: Optional[Callable[["TranslationRequest", "TranslationResult"], "TranslationResult"]] = None,
    ) -> None:
        self._name = name
        self._privacy = privacy
        self._key_provider = key_provider
        self._timeout_s = timeout_s
        self._auditor = auditor

    @property
    def name(self) -> str:
        return self._name

    @property
    def privacy(self) -> "PrivacyController":
        return self._privacy

    def _classify(self, status_code: int, body: str) -> "ProviderError":
        """Классифицировать HTTP-код в соответствующее исключение провайдера."""
        _ = body  # не используется, но может понадобиться для расширенной классификации
        if status_code in (401, 403):
            return ProviderAuthError(f"auth failed: {status_code}")
        if status_code == 429:
            return ProviderRateLimited(f"rate limited: {status_code}")
        if 500 <= status_code < 600:
            return ProviderUnavailable(f"server error: {status_code}")
        if status_code in (400, 422):
            return ProviderResponseInvalid(f"invalid response: {status_code}")
        return ProviderError(f"provider error: {status_code}")

    async def translate(self, req: "TranslationRequest", *, fence: "Fence") -> "TranslationResult":
        """Основной метод перевода: проверка приватности, вызов провайдера, аудит."""
        # 1. Проверка приватности: требуем право на TEXT_TO_CLOUD + fence
        if not self._privacy.allows(Capability.TEXT_TO_CLOUD):
            raise PrivacyViolation(Capability.TEXT_TO_CLOUD.value, self._privacy.profile.value)

        # Захватываем fence ПЕРЕД вызовом (require — атомарная проверка + захват)
        fence = self._privacy.require(Capability.TEXT_TO_CLOUD)

        # 2. Получаем ключ провайдера
        api_key = self._key_provider()
        if not api_key:
            raise ProviderAuthError("empty API key")

        # 3. Вызов провайдера с таймаутом
        try:
            raw_result = await asyncio.wait_for(
                self._call(req, api_key),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            raise ProviderUnavailable(f"timeout {self._timeout_s}s") from None

        # 4. Валидация fence ПОСЛЕ сетевого вызова, ДО использования результата
        self._privacy.validate(fence, Capability.TEXT_TO_CLOUD)

        # 5. Парсим ответ
        result = self._parse(req, raw_result)

        # Проверка на пустой ответ при непустом входе
        if not result.translation_raw and req.text:
            raise ProviderResponseInvalid("empty translation_raw for non-empty input")

        # 6. Аудит — всегда, через отложенный импорт при необходимости
        auditor = self._auditor
        if auditor is None:
            from app.translation.prompts import audit as auditor  # отложенный импорт

        return auditor(req, result)

    @abc.abstractmethod
    async def _call(self, req: "TranslationRequest", api_key: str) -> str:
        """Выполнить HTTP-запрос к провайдеру и вернуть сырой ответ (строка).
        Должен быть реализован в подклассе.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def _parse(self, req: "TranslationRequest", raw: str) -> "TranslationResult":
        """Разобрать сырой ответ провайдера в TranslationResult.
        Должен быть реализован в подклассе.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Закрыть ресурсы (например, закрыть HTTP-сессию).
        По умолчанию ничего не делает.
        """
        pass