"""app/translation/base.py — базовые интерфейсы провайдеров перевода. Задача D1 роадмапа."""

from __future__ import annotations

import abc
from enum import Enum
from typing import Callable, Protocol, Tuple, Literal, Optional, runtime_checkable

from app.privacy import PrivacyController, Fence, Capability
from app.errors import SpeechLocalError


# ------------------------------------------------------------------ enums


class TranslationMode(str, Enum):
    LIVE_LITERAL = "live_literal"
    LIVE_SAFE = "live_safe"
    POST_CLEAN = "post_clean"


# ------------------------------------------------------------------ data classes


from dataclasses import dataclass


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
    privacy: PrivacyController

    async def translate(self, req: TranslationRequest, *, fence: Fence) -> TranslationResult:
        """Перевести текст."""
        ...

    async def close(self) -> None:
        """Освободить ресурсы."""
        ...


# ------------------------------------------------------------------ base class


class BaseTranslationProvider(abc.ABC):
    """Общая обвязка. Наследники реализуют _call и _parse."""

    Auditor = Callable[[TranslationRequest, TranslationResult], TranslationResult]

    def __init__(
        self,
        name: str,
        privacy: PrivacyController,
        key_provider: Callable[[], str],
        *,
        timeout_s: float = 15.0,
        auditor: Optional[Auditor] = None,
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
    def privacy(self) -> PrivacyController:
        return self._privacy

    async def translate(self, req: TranslationRequest, *, fence: Fence) -> TranslationResult:
        """Основной метод перевода: проверка приватности, вызов провайдера, аудит."""
        # 1. Проверка приватности: требуем право на TEXT_TO_CLOUD и проверяем fence.
        if not self._privacy.allows(Capability.TEXT_TO_CLOUD):
            raise PermissionError("Privacy does not allow TEXT_TO_CLOUD")
        # Validate the fence (check generation)
        self._privacy.validate(fence, Capability.TEXT_TO_CLOUD)

        # 2. Получаем ключ провайдера
        api_key = self._key_provider()

        # 3. Формируем запрос и вызываем провайдера (должен быть реализован в подклассе)
        try:
            raw_result = await self._call(req, api_key)
        except Exception as e:
            # Ошибки при вызове провайдера должны быть обработаны и преобразованы в соответствующие исключения.
            # Поскольку мы не знаем конкретных ошибок, мы просто повторно вызываем их.
            # В реальном коде, здесь должна быть логика преобразования ошибок.
            raise

        # 4. Парсим ответ
        result = self._parse(raw_result)

        # 5. Применяем аудитор, если он предоставлен
        if self._auditor is not None:
            result = self._auditor(req, result)

        return result

    @abc.abstractmethod
    async def _call(self, req: TranslationRequest, api_key: str) -> str:
        """Выполнить HTTP-запрос к провайдеру и вернуть сырой ответ (строка).
        Должен быть реализован в подклассе.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def _parse(self, raw_response: str) -> TranslationResult:
        """Разобрать сырой ответ провайдера в TranslationResult.
        Должен быть реализован в подклассе.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Закрыть ресурсы (например, закрыть HTTP-сессию).
        По умолчанию ничего не делает.
        """
        pass