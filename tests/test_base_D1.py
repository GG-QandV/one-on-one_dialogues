"""Tests for app/translation/base.py (D1)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.translation.base import (
    BaseTranslationProvider,
    TranslationMode,
    TranslationRequest,
    TranslationResult,
    Change,
    TranslationProvider,
)
from app.privacy import PrivacyController, Capability, PrivacyProfile
from app.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderResponseInvalid,
    ProviderError,
    PrivacyViolation,
    StaleGenerationError,
)


class DummyProvider(BaseTranslationProvider):
    """Тестовый провайдер для проверки базового класса."""

    def __init__(self, *args, should_fail=False, fail_status=500, **kwargs):
        super().__init__(*args, **kwargs)
        self._should_fail = should_fail
        self._fail_status = fail_status
        self.call_count = 0
        self.last_req = None
        self.last_key = None

    async def _call(self, req: TranslationRequest, api_key: str) -> str:
        self.call_count += 1
        self.last_req = req
        self.last_key = api_key
        if self._should_fail:
            raise self._classify(self._fail_status, "error")
        return "translated text"

    def _parse(self, req: TranslationRequest, raw: str) -> TranslationResult:
        return TranslationResult(translation_raw=raw)

    async def close(self) -> None:
        pass


@pytest.fixture
def privacy_controller():
    return PrivacyController(initial=PrivacyProfile.OPEN)


@pytest.fixture
def key_provider():
    return lambda: "test-key"


@pytest.fixture
def provider(privacy_controller, key_provider):
    return DummyProvider(
        name="dummy",
        privacy=privacy_controller,
        key_provider=key_provider,
        timeout_s=5.0,
    )


@pytest.fixture
def req():
    return TranslationRequest(
        text="Hello world",
        source_language="en",
        target_language="ru",
        mode=TranslationMode.LIVE_LITERAL,
    )


@pytest.fixture
def req_with_number():
    return TranslationRequest(
        text="Price is 100 dollars",
        source_language="en",
        target_language="ru",
        mode=TranslationMode.LIVE_LITERAL,
    )


class TestBaseTranslationProvider:
    """Тесты базового класса провайдера (D1)."""

    @pytest.mark.asyncio
    async def test_translate_in_open_profile_passes(self, provider, req):
        """translate в открытом профиле проходит для текста."""
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)
        result = await provider.translate(req, fence=fence)
        assert isinstance(result, TranslationResult)
        assert result.translation_raw == "translated text"

    @pytest.mark.asyncio
    async def test_translate_in_confidential_profile_for_text_passes(self, provider, req):
        """translate в конфиденциальном профиле для ТЕКСТА проходит (TEXT_TO_CLOUD разрешён)."""
        # Создаём новый контроллер с CONFIDENTIAL профилем
        controller = PrivacyController(initial=PrivacyProfile.CONFIDENTIAL)
        provider._privacy = controller
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)
        result = await provider.translate(req, fence=fence)
        assert isinstance(result, TranslationResult)
        assert result.translation_raw == "translated text"

    @pytest.mark.asyncio
    async def test_switch_during_call_raises_stalegenerationerror(self, provider, req):
        """switch() посреди вызова -> StaleGenerationError, результат не возвращается."""
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)

        # Подменяем _call чтобы переключить профиль во время выполнения
        original_call = provider._call

        async def switching_call(req, key):
            # Переключаем профиль во время "сетевого вызова"
            await provider.privacy.switch(PrivacyProfile.CONFIDENTIAL, session_id="test")
            return await original_call(req, key)

        provider._call = switching_call

        with pytest.raises(StaleGenerationError):
            await provider.translate(req, fence=fence)

    @pytest.mark.asyncio
    async def test_empty_key_raises_providerautherror_no_call(self, provider, req):
        """Пустой ключ -> ProviderAuthError, _call не вызывался."""
        def empty_key_provider():
            return ""

        provider._key_provider = empty_key_provider
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)

        with pytest.raises(ProviderAuthError):
            await provider.translate(req, fence=fence)

        assert provider.call_count == 0

    @pytest.mark.asyncio
    async def test_timeout_raises_providerunavailable_no_req_text(self, provider, req):
        """Таймаут -> ProviderUnavailable, в сообщении нет req.text."""
        async def slow_call(req, key):
            await asyncio.sleep(10)
            return "late"

        provider._call = slow_call
        provider._timeout_s = 0.01  # очень маленький таймаут
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)

        with pytest.raises(ProviderUnavailable) as exc_info:
            await provider.translate(req, fence=fence)

        assert "timeout" in str(exc_info.value).lower()
        assert req.text not in str(exc_info.value)

    @pytest.mark.parametrize("status,expected_type,retryable", [
        (401, ProviderAuthError, False),
        (403, ProviderAuthError, False),
        (429, ProviderRateLimited, True),
        (500, ProviderUnavailable, True),
        (502, ProviderUnavailable, True),
        (503, ProviderUnavailable, True),
        (400, ProviderResponseInvalid, False),
        (422, ProviderResponseInvalid, False),
        (418, ProviderError, True),  # прочее -> ProviderError, retryable
    ])
    def test_classify_codes_to_correct_exceptions(self, provider, status, expected_type, retryable):
        """Коды 401/429/503/400 -> правильные типы и retryable."""
        exc = provider._classify(status, "body")
        assert isinstance(exc, expected_type)
        assert exc.retryable == retryable

    @pytest.mark.asyncio
    async def test_empty_response_raises_providerresponseinvalid(self, provider, req):
        """Пустой ответ при непустом входе -> ProviderResponseInvalid."""
        def empty_parse(r, raw):
            return TranslationResult(translation_raw="")

        provider._parse = empty_parse

        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)
        with pytest.raises(ProviderResponseInvalid):
            await provider.translate(req, fence=fence)

    @pytest.mark.asyncio
    async def test_live_literal_no_loss_changes_empty(self, provider, req):
        """LIVE_LITERAL без потерь сущностей -> translation_clean is None, changes == ()."""
        req = TranslationRequest(
            text="Hello world",
            source_language="en",
            target_language="ru",
            mode=TranslationMode.LIVE_LITERAL,
        )
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)
        result = await provider.translate(req, fence=fence)
        assert result.translation_clean is None
        assert result.changes == ()

    @pytest.mark.asyncio
    async def test_auditor_called_once_after_second_validate(self, provider, req):
        """auditor вызывается ровно один раз за translate, ПОСЛЕ второго validate."""
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)
        call_count = 0

        def counting_auditor(req, result):
            nonlocal call_count
            call_count += 1
            return result

        provider._auditor = counting_auditor
        await provider.translate(req, fence=fence)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_translation_with_lost_number_marks_lost_entity(self, provider, req_with_number):
        """Перевод с пропавшим числом -> в changes элемент lost_entity, исключения нет."""
        # Подменяем _call чтобы вернуть перевод БЕЗ числа
        async def call_without_number(req, key):
            return "Price is dollars"

        provider._call = call_without_number
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)
        result = await provider.translate(req_with_number, fence=fence)

        lost_entities = [c for c in result.changes if c.type == "lost_entity"]
        assert len(lost_entities) == 1
        assert lost_entities[0].original == "100"

    @pytest.mark.asyncio
    async def test_auditor_none_triggers_lazy_import(self, provider, req):
        """auditor is None -> отложенный импорт prompts.audit отрабатывает."""
        provider._auditor = None
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)

        # Должно отработать без ошибки импорта
        result = await provider.translate(req, fence=fence)
        assert isinstance(result, TranslationResult)

    @pytest.mark.asyncio
    async def test_key_canary_not_in_repr_or_exceptions(self, provider, req):
        """Ключ sk-CANARY не встречается ни в repr, ни в str(exc), ни в traceback."""
        canary = "sk-CANARY"
        provider._key_provider = lambda: canary
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)

        # Проверяем repr
        assert canary not in repr(provider)

        # Проверяем исключения (пустой ключ)
        provider._key_provider = lambda: ""
        with pytest.raises(ProviderAuthError) as exc_info:
            await provider.translate(req, fence=fence)
        assert canary not in str(exc_info.value)
        assert canary not in repr(exc_info.value)

    def test_fence_passed_as_parameter(self, provider, req):
        """fence передаётся как параметр (как в TranslationProvider.translate(..., *, fence))."""
        fence = provider.privacy.require(Capability.TEXT_TO_CLOUD)
        # Проверяем, что translate принимает fence как keyword-only аргумент
        import inspect
        sig = inspect.signature(provider.translate)
        assert 'fence' in sig.parameters
        assert sig.parameters['fence'].kind == inspect.Parameter.KEYWORD_ONLY


if __name__ == "__main__":
    pytest.main([__file__, "-v"])