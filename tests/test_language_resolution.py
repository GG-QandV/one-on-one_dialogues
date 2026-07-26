"""Tests for stt/language.py (C8)."""

import pytest

from app.stt.language import LanguageDecision, resolve


def test_same_language_no_conflict():
    """configured=\"en\", detected=\"en\" → conflict=False, note is None"""
    decision = resolve("en", "en", True)
    assert decision.effective == "en"
    assert decision.configured == "en"
    assert decision.detected == "en"
    assert decision.conflict is False
    assert decision.note is None


def test_different_language_conflict():
    """configured=\"en\", detected=\"es\" → conflict=True, effective=\"en\" """
    decision = resolve("en", "es", True)
    assert decision.effective == "en"
    assert decision.configured == "en"
    assert decision.detected == "es"
    assert decision.conflict is True
    assert decision.note == "Задан английский, распознан испанский."


def test_normalized_language_no_conflict():
    """configured=\"en\", detected=\"en-US\" → conflict=False (нормализация)"""
    decision = resolve("en", "en-US", True)
    assert decision.effective == "en"
    assert decision.configured == "en"
    assert decision.detected == "en-US"
    assert decision.conflict is False
    assert decision.note is None


def test_synonym_language_no_conflict():
    """configured=\"uk\", detected=\"ua\" → conflict=False (синоним)"""
    decision = resolve("uk", "ua", True)
    assert decision.effective == "uk"
    assert decision.configured == "uk"
    assert decision.detected == "ua"
    assert decision.conflict is False
    assert decision.note is None


def test_close_language_conflict_with_note():
    """configured=\"ru\", detected=\"uk\" → conflict=True, в note есть пометка о близких языках"""
    decision = resolve("ru", "uk", True)
    assert decision.effective == "ru"
    assert decision.configured == "ru"
    assert decision.detected == "uk"
    assert decision.conflict is True
    assert "Близкие языки" in decision.note
    assert decision.note.startswith("Задан русский, распознан украинский.")


def test_autodetect_disabled_no_conflict():
    """autodetect_enabled=False, detected=\"es\" → conflict=False"""
    decision = resolve("en", "es", False)
    assert decision.effective == "en"
    assert decision.configured == "en"
    assert decision.detected == "es"
    assert decision.conflict is False
    assert decision.note is None


def test_configured_auto():
    """configured=\"auto\", detected=\"es\" → effective=\"es\", conflict=False"""
    decision = resolve("auto", "es", True)
    assert decision.effective == "es"
    assert decision.configured == "auto"
    assert decision.detected == "es"
    assert decision.conflict is False
    assert decision.note is None


def test_detected_none_no_conflict():
    """detected=None → conflict=False, effective=configured"""
    decision = resolve("en", None, True)
    assert decision.effective == "en"
    assert decision.configured == "en"
    assert decision.detected is None
    assert decision.conflict is False
    assert decision.note is None


def test_detected_empty_string_no_conflict():
    """detected=\"\" → conflict=False, effective=configured"""
    decision = resolve("en", "", True)
    assert decision.effective == "en"
    assert decision.configured == "en"
    assert decision.detected == ""
    assert decision.conflict is False
    assert decision.note is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])