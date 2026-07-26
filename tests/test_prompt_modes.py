"""Tests for app/translation/prompts.py (D4)."""

import pytest

from app.translation.prompts import (
    build,
    format_context,
    validate_response,
    filler_changes,
    detect_drift,
    audit,
    LANGUAGE_NAMES,
    FILLERS,
)
from app.translation.base import TranslationMode, TranslationRequest, TranslationResult, Change


def test_build_live_literal():
    """Test building prompts for LIVE_LITERAL mode."""
    req = TranslationRequest(
        text="Hello world",
        source_language="en",
        target_language="ru",
        mode=TranslationMode.LIVE_LITERAL,
    )
    system, user = build(TranslationMode.LIVE_LITERAL, req)
    # From prompts_spec_11.json
    assert "Переведи текст с English на Russian." in system
    assert "Не добавляй и не удаляй факты." in system
    assert "Сохрани числа, суммы, валюты, даты, имена, названия компаний," in system
    assert "артикулы, номера, ссылки и единицы измерения без изменений." in system
    assert "Не объясняй перевод. Верни только перевод." in system
    assert "Текст для перевода:" in user
    assert "Hello world" in user


def test_build_live_safe():
    """Test building prompts for LIVE_SAFE mode."""
    req = TranslationRequest(
        text="Hello world",
        source_language="en",
        target_language="ru",
        mode=TranslationMode.LIVE_SAFE,
    )
    system, user = build(TranslationMode.LIVE_SAFE, req)
    # From prompts_spec_11.json
    assert "Переведи текст с English на Russian." in system
    assert "Удаляй только отдельные очевидные междометия и слова-паразиты:" in system
    assert "«ээ», «эээ», «эм», «эмм», «мм», «ммм», «uh», «um», «er»." in system
    assert "Не удаляй слова, если они могут менять смысл." in system
    assert "Сохраняй все факты, числа, имена, даты и названия." in system
    assert "Верни только перевод." in system
    assert "Текст для перевода:" in user
    assert "Hello world" in user


def test_build_post_clean():
    """Test building prompts for POST_CLEAN mode."""
    req = TranslationRequest(
        text="Hello world",
        source_language="en",
        target_language="ru",
        mode=TranslationMode.POST_CLEAN,
    )
    system, user = build(TranslationMode.POST_CLEAN, req)
    # From prompts_spec_11.json
    assert "Отредактируй стенограмму без изменения смысла." in system
    assert "Удали очевидные слова-паразиты, ложные старты и бессмысленные повторы." in system
    assert "Восстанови пунктуацию и абзацы." in system
    assert "Не меняй числа, даты, валюты, имена, названия, ссылки," in system
    assert "обязательства, решения и степень уверенности говорящего." in system
    assert "Верни JSON: {\"clean_text\": \"...\", \"changes\": [...]}" in system
    assert "Текст для перевода:" in user
    assert "Hello world" in user


def test_build_unknown_language():
    """Test building prompts with unknown language code."""
    req = TranslationRequest(
        text="Hello world",
        source_language="xx",
        target_language="yy",
        mode=TranslationMode.LIVE_LITERAL,
    )
    system, user = build(TranslationMode.LIVE_LITERAL, req)
    assert "с xx на yy" in system  # Should use the code as is


def test_build_with_context():
    """Test building prompts with context."""
    req = TranslationRequest(
        text="Hello world",
        source_language="en",
        target_language="ru",
        mode=TranslationMode.LIVE_LITERAL,
        context=("Previous sentence.", "Another one."),
    )
    system, user = build(TranslationMode.LIVE_LITERAL, req)
    assert "Контекст предыдущих реплик (НЕ переводить, только для понимания):" in user
    assert "1) Previous sentence." in user
    assert "2) Another one." in user
    assert "Текст для перевода:" in user
    assert "Hello world" in user


def test_build_empty_context():
    """Test building prompts with empty context."""
    req = TranslationRequest(
        text="Hello world",
        source_language="en",
        target_language="ru",
        mode=TranslationMode.LIVE_LITERAL,
        context=(),
    )
    system, user = build(TranslationMode.LIVE_LITERAL, req)
    assert "Контекст предыдущих реплик (НЕ переводить, только для понимания):" not in user
    assert "Текст для перевода:" in user
    assert "Hello world" in user


def test_format_context():
    """Test format_context function."""
    assert format_context(()) == ""
    assert format_context(("hello",)) == "Контекст предыдущих реплик (НЕ переводить, только для понимания):\n1) hello"
    assert format_context(("a", "b")) == "Контекст предыдущих реплик (НЕ переводить, только для понимания):\n1) a\n2) b"


def test_validate_response_live_literal():
    """Test validate_response for LIVE_LITERAL mode."""
    raw = "  перевод  "
    result = validate_response(TranslationMode.LIVE_LITERAL, raw)
    assert result.translation_raw == "  перевод  "
    assert result.translation_clean is None
    assert result.changes == ()


def test_validate_response_live_literal_empty():
    """Test validate_response for LIVE_LITERAL mode with empty string."""
    raw = ""
    with pytest.raises(ValueError, match="Invalid JSON"):
        validate_response(TranslationMode.LIVE_LITERAL, raw)


def test_validate_response_live_safe():
    """Test validate_response for LIVE_SAFE mode."""
    raw = "  перевод  "
    result = validate_response(TranslationMode.LIVE_SAFE, raw)
    assert result.translation_raw == "  перевод  "
    assert result.translation_clean is None
    assert result.changes == ()


def test_validate_response_post_clean_valid():
    """Test validate_response for POST_CLEAN mode with valid JSON."""
    raw = '{"translation": "clean text", "changes": [{"type": "filler_removed", "original": "эх", "replacement": ""}]}'
    result = validate_response(TranslationMode.POST_CLEAN, raw)
    assert result.translation_raw == raw
    assert result.translation_clean == "clean text"
    assert len(result.changes) == 1
    assert result.changes[0].type == "filler_removed"
    assert result.changes[0].original == "эх"
    assert result.changes[0].replacement == ""


def test_validate_response_post_clean_missing_translation():
    """Test validate_response for POST_CLEAN mode missing translation field."""
    raw = '{"changes": []}'
    with pytest.raises(ValueError, match="Missing 'translation' field"):
        validate_response(TranslationMode.POST_CLEAN, raw)


def test_validate_response_post_clean_invalid_json():
    """Test validate_response for POST_CLEAN mode with invalid JSON."""
    raw = "not json"
    with pytest.raises(ValueError, match="Invalid JSON"):
        validate_response(TranslationMode.POST_CLEAN, raw)


def test_filler_changes_live_safe():
    """Test filler_changes for LIVE_SAFE mode."""
    req = TranslationRequest(
        text="ээ эм мм uh um er",
        source_language="en",
        target_language="ru",
        mode=TranslationMode.LIVE_SAFE,
    )
    changes = filler_changes(req)
    # We expect 6 changes (one for each filler)
    assert len(changes) == 6
    # Check that each change is of type filler_removed and replacement is empty
    for change in changes:
        assert change.type == "filler_removed"
        assert change.replacement == ""


def test_filler_changes_not_live_safe():
    """Test filler_changes for non-LIVE_SAFE modes."""
    for mode in [TranslationMode.LIVE_LITERAL, TranslationMode.POST_CLEAN]:
        req = TranslationRequest(
            text="ээ эм мм uh um er",
            source_language="en",
            target_language="ru",
            mode=mode,
        )
        changes = filler_changes(req)
        assert changes == ()


def test_detect_drift_number():
    """Test detect_drift for missing number."""
    req = TranslationRequest(
        text="сумма 30 000 евро",
        source_language="ru",
        target_language="en",
        mode=TranslationMode.LIVE_LITERAL,
    )
    # Translation missing the number
    translation = "sum of euros"
    changes = detect_drift(req, translation)
    # We expect one lost_entity for the number
    assert len(changes) == 1
    assert changes[0].type == "lost_entity"
    # The number is normalized to "30000"
    assert changes[0].original == "30000"
    assert changes[0].replacement == ""


def test_detect_drift_number_present():
    """Test detect_drift for present number."""
    req = TranslationRequest(
        text="сумма 30 000 евро",
        source_language="ru",
        target_language="en",
        mode=TranslationMode.LIVE_LITERAL,
    )
    # Translation includes the number
    translation = "sum of 30000 euros"
    changes = detect_drift(req, translation)
    assert changes == ()


def test_detect_drift_url():
    """Test detect_drift for missing URL."""
    req = TranslationRequest(
        text="посетите сайт https://example.com",
        source_language="ru",
        target_language="en",
        mode=TranslationMode.LIVE_LITERAL,
    )
    translation = "visit the site"
    changes = detect_drift(req, translation)
    assert len(changes) == 1
    assert changes[0].type == "lost_entity"
    assert changes[0].original == "https://example.com"
    assert changes[0].replacement == ""


def test_detect_drift_url_present():
    """Test detect_drift for present URL."""
    req = TranslationRequest(
        text="посетите сайт https://example.com",
        source_language="ru",
        target_language="en",
        mode=TranslationMode.LIVE_LITERAL,
    )
    translation = "visit the site https://example.com"
    changes = detect_drift(req, translation)
    assert changes == ()


def test_detect_drift_length():
    """Test detect_drift for length mismatch."""
    req = TranslationRequest(
        text="слово " * 40,  # 40 words
        source_language="ru",
        target_language="en",
        mode=TranslationMode.LIVE_LITERAL,
    )
    translation = "word " * 20  # 20 words -> ratio 0.5 < 0.6
    changes = detect_drift(req, translation)
    assert len(changes) == 1
    assert changes[0].type == "lost_entity"
    assert changes[0].original == ""  # Empty for length mismatch per fixture
    assert changes[0].replacement == ""


def test_detect_drift_length_ok():
    """Test detect_drift for length ratio OK."""
    req = TranslationRequest(
        text="слово " * 40,  # 40 words
        source_language="ru",
        target_language="en",
        mode=TranslationMode.LIVE_LITERAL,
    )
    translation = "word " * 25  # 25 words -> ratio 0.625 >= 0.6
    changes = detect_drift(req, translation)
    assert changes == ()


def test_audit_combines_filler_and_drift():
    """Test audit combines filler_changes and detect_drift."""
    req = TranslationRequest(
        text="ээ сумма 30 000 евро",
        source_language="ru",
        target_language="en",
        mode=TranslationMode.LIVE_SAFE,  # LIVE_SAFE to get filler changes
    )
    # Translation missing the number and the filler
    translation = "sum of euros"
    # First, get a base result (as if from validate_response)
    base_result = TranslationResult(
        translation_raw="sum of euros",
        translation_clean=None,
        changes=(),
    )
    audited = audit(req, base_result)
    # We expect two changes: one for filler "ээ" and one for number "30000"
    assert len(audited.changes) == 2
    # Check that we have one filler_removed and one lost_entity
    filler_changes = [c for c in audited.changes if c.type == "filler_removed"]
    lost_entities = [c for c in audited.changes if c.type == "lost_entity"]
    assert len(filler_changes) == 1
    assert filler_changes[0].original == "ээ"
    assert filler_changes[0].replacement == ""
    assert len(lost_entities) == 1
    assert lost_entities[0].original == "30000"
    assert lost_entities[0].replacement == ""


def test_audit_live_literal_only_drift():
    """Test audit for LIVE_LITERAL only adds drift changes (no filler)."""
    req = TranslationRequest(
        text="ээ сумма 30 000 евро",
        source_language="ru",
        target_language="en",
        mode=TranslationMode.LIVE_LITERAL,
    )
    # Translation missing the number but filler is not checked in LIVE_LITERAL
    translation = "sum of euros"
    base_result = TranslationResult(
        translation_raw="sum of euros",
        translation_clean=None,
        changes=(),
    )
    audited = audit(req, base_result)
    # Only the drift change (for the number) should be present
    assert len(audited.changes) == 1
    assert audited.changes[0].type == "lost_entity"
    assert audited.changes[0].original == "30000"
    assert audited.changes[0].replacement == ""


def test_language_names():
    """Test LANGUAGE_NAMES mapping."""
    assert LANGUAGE_NAMES["ru"] == "Russian"
    assert LANGUAGE_NAMES["en"] == "English"
    assert LANGUAGE_NAMES["xx"] == "xx"  # Not in the dict, so get returns the key? Actually, we use .get with fallback to the key.


def test_fillers():
    """Test FILLERS tuple."""
    assert "ээ" in FILLERS
    assert "uh" in FILLERS
    assert "er" in FILLERS
    assert len(FILLERS) == 9