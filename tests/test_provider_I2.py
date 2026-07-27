"""Tests for app/drafts/provider.py (I2)."""

import json
import pytest

from app.drafts.provider import (
    DraftProvider,
    DraftProviderConfig,
    DraftRequest,
    LibraryPort,
)
from app.drafts.guardrails import DraftCandidate
from app.drafts.library import LibraryContext
from app.translation.base import (
    TranslationRequest,
    TranslationResult,
    TranslationMode,
)
from app.privacy import PrivacyController, PrivacyProfile, Capability
from app.errors import ProviderRateLimited, InvariantViolation


# --------------------------------------------------------------- fakes

class FakeLibrary:
    def __init__(self, text: str | None):
        self._text = text
        self.calls: list[str] = []

    async def get(self, context_id: str):
        self.calls.append(context_id)
        if self._text is None:
            raise InvariantViolation("library_context_not_found")
        return LibraryContext(
            id=context_id, name="test", domain=None,
            content_text=self._text,
            token_estimate=10, updated_at="2025-01-01T00:00:00.000",
        )


class FakeProvider:
    """Подменённый TranslationProvider: возвращает заданный JSON-ответ."""

    name = "fake"

    def __init__(self, response_json: str | None = None,
                 raise_exc: Exception | None = None):
        self.privacy = PrivacyController(initial=PrivacyProfile.OPEN)
        self._response = response_json
        self._raise = raise_exc
        self.last_request: TranslationRequest | None = None
        self.require_called = False

    async def translate(self, req: TranslationRequest, *, fence):
        self.last_request = req
        if self._raise is not None:
            raise self._raise
        return TranslationResult(translation_raw=self._response or "")

    async def close(self):
        pass


def _fence(provider: FakeProvider):
    return provider.privacy.require(Capability.TEXT_TO_CLOUD)


def _good_response(answer="Стандартная цена 30000 рублей за лицензию.",
                   sources=("Прайс-лист",), has_gaps=False, gap_note=None):
    return json.dumps({
        "answer": answer, "sources": list(sources),
        "has_gaps": has_gaps, "gap_note": gap_note,
    }, ensure_ascii=False)


# --------------------------------------------------------------- tests

@pytest.mark.asyncio
async def test_generates_candidate_with_fact_in_library():
    """Вопрос с фактом → DraftCandidate, непустые sources, has_gaps=False."""
    provider = FakeProvider(_good_response())
    lib = FakeLibrary("Прайс-лист: цена 30000 рублей за лицензию.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Сколько стоит лицензия?", "en", "lib1")
    cand = await i2.generate(req, fence=_fence(provider))

    assert isinstance(cand, DraftCandidate)
    assert cand.sources == ("Прайс-лист",)
    assert cand.has_gaps_claimed is False
    assert cand.draft_ru  # непустой


@pytest.mark.asyncio
async def test_gap_when_no_fact_in_library():
    """Вопрос без факта → gap_marker в тексте, has_gaps=True, sources пусты."""
    provider = FakeProvider(_good_response(
        answer="нет данных по этому вопросу", sources=(), has_gaps=True,
        gap_note="вопрос вне справки"))
    lib = FakeLibrary("Прайс-лист: цена 30000.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Какая гарантия?", "en", "lib1")
    cand = await i2.generate(req, fence=_fence(provider))

    assert cand.has_gaps_claimed is True
    assert cand.sources == ()


@pytest.mark.asyncio
async def test_library_inserted_whole_into_prompt():
    """Библиотека и вопрос вставляются в промпт (без инструкций)."""
    lib_text = "УНИКАЛЬНЫЙ_МАРКЕР_БИБЛИОТЕКИ цена 30000."
    provider = FakeProvider(_good_response())
    lib = FakeLibrary(lib_text)
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Сколько?", "en", "lib1")
    await i2.generate(req, fence=_fence(provider))

    assert f"СПРАВКА:\n{lib_text}" in provider.last_request.text
    assert "ВОПРОС СОБЕСЕДНИКА:\nСколько?" in provider.last_request.text


@pytest.mark.asyncio
async def test_draft_is_russian_regardless_of_target():
    """Запрос к LLM на генерацию (ru->ru), не перевод на target."""
    provider = FakeProvider(_good_response())
    lib = FakeLibrary("цена 30000.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Сколько?", "pl", "lib1")
    await i2.generate(req, fence=_fence(provider))

    assert provider.last_request.source_language == "ru"
    assert provider.last_request.target_language == "ru"


@pytest.mark.asyncio
async def test_gap_marker_in_text_forces_has_gaps():
    """gap_marker в тексте → has_gaps=True, даже если модель вернула has_gaps=False."""
    provider = FakeProvider(_good_response(
        answer="К сожалению нет данных в справке.",
        sources=(), has_gaps=False))   # модель забыла флаг
    lib = FakeLibrary("цена 30000.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Гарантия?", "en", "lib1")
    cand = await i2.generate(req, fence=_fence(provider))

    assert cand.has_gaps_claimed is True   # форсировано по факту


@pytest.mark.asyncio
async def test_unverified_number_not_stripped_by_i2():
    """Число вне библиотеки НЕ удаляется I2 — уходит в кандидат как есть."""
    provider = FakeProvider(_good_response(
        answer="Цена 99999 рублей.", sources=("Прайс",)))
    lib = FakeLibrary("Прайс: 30000.")   # 99999 нет в библиотеке
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Цена?", "en", "lib1")
    cand = await i2.generate(req, fence=_fence(provider))

    assert "99999" in cand.draft_ru   # I2 не тронул; отбракует I5


@pytest.mark.asyncio
async def test_empty_question_returns_none_no_llm():
    """Пустой вопрос → None, LLM не вызывался."""
    provider = FakeProvider(_good_response())
    lib = FakeLibrary("цена 30000.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "   ", "en", "lib1")
    cand = await i2.generate(req, fence=_fence(provider))

    assert cand is None
    assert provider.last_request is None   # не вызывался


@pytest.mark.asyncio
async def test_empty_library_returns_none_no_llm():
    """Пустая библиотека → None, LLM не вызывался."""
    provider = FakeProvider(_good_response())
    lib = FakeLibrary(None)
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Сколько?", "en", "lib1")
    cand = await i2.generate(req, fence=_fence(provider))

    assert cand is None
    assert provider.last_request is None


@pytest.mark.asyncio
async def test_provider_error_propagates():
    """ProviderRateLimited пробрасывается наружу."""
    provider = FakeProvider(raise_exc=ProviderRateLimited("429"))
    lib = FakeLibrary("цена 30000.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Сколько?", "en", "lib1")
    with pytest.raises(ProviderRateLimited):
        await i2.generate(req, fence=_fence(provider))


@pytest.mark.asyncio
async def test_require_not_called_inside():
    """require() внутри I2 не вызывается — fence приходит параметром."""
    provider = FakeProvider(_good_response())

    # privacy с падающим require: если I2 вызовет require — тест упадёт
    class StrictPrivacy(PrivacyController):
        def require(self, cap):
            raise AssertionError("I2 must not call require()")

    provider.privacy = StrictPrivacy(initial=PrivacyProfile.OPEN)
    lib = FakeLibrary("цена 30000.")
    i2 = DraftProvider(provider, lib)

    # fence добываем напрямую, минуя провайдер
    from app.privacy import Fence
    fence = Fence(PrivacyProfile.OPEN, 0)

    req = DraftRequest("s1", "seg1", "Сколько?", "en", "lib1")
    cand = await i2.generate(req, fence=fence)   # не должно звать require
    assert cand is not None


@pytest.mark.asyncio
async def test_malformed_json_returns_none():
    """Битый JSON от модели → None (parse_failed в snapshot)."""
    provider = FakeProvider("это не JSON вообще")
    lib = FakeLibrary("цена 30000.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Сколько?", "en", "lib1")
    cand = await i2.generate(req, fence=_fence(provider))

    assert cand is None
    assert i2.snapshot()["parse_failed"] == 1


@pytest.mark.asyncio
async def test_json_fence_wrapper_stripped():
    """```json-обёртка снимается перед разбором."""
    wrapped = "```json\n" + _good_response() + "\n```"
    provider = FakeProvider(wrapped)
    lib = FakeLibrary("Прайс: 30000.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Цена?", "en", "lib1")
    cand = await i2.generate(req, fence=_fence(provider))

    assert cand is not None
    assert cand.sources == ("Прайс-лист",)


@pytest.mark.asyncio
async def test_candidate_carries_trigger_and_target():
    """Кандидат несёт trigger_segment_id и target_language из запроса."""
    provider = FakeProvider(_good_response())
    lib = FakeLibrary("цена 30000.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("sX", "segY", "Сколько?", "es", "lib1")
    cand = await i2.generate(req, fence=_fence(provider))

    assert cand.trigger_segment_id == "segY"
    assert cand.target_language == "es"
    assert cand.session_id == "sX"


@pytest.mark.asyncio
async def test_uses_draft_mode_not_postclean():
    """Запрос к LLM в режиме DRAFT, не POST_CLEAN."""
    provider = FakeProvider(_good_response())
    lib = FakeLibrary("цена 30000.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Сколько?", "en", "l1")
    await i2.generate(req, fence=_fence(provider))

    assert provider.last_request.mode == TranslationMode.DRAFT


@pytest.mark.asyncio
async def test_snapshot_counts_generated():
    """snapshot считает сгенерированные."""
    provider = FakeProvider(_good_response())
    lib = FakeLibrary("цена 30000.")
    i2 = DraftProvider(provider, lib)

    req = DraftRequest("s1", "seg1", "Сколько?", "en", "lib1")
    await i2.generate(req, fence=_fence(provider))
    await i2.generate(req, fence=_fence(provider))

    assert i2.snapshot()["generated"] == 2
