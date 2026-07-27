"""Tests for app/drafts/trigger.py (I3)."""

import pytest

from app.drafts.trigger import is_question, score, TriggerConfig


def test_question_with_question_mark():
    """«Сколько это будет стоить?» / ru → (True, 0.95)"""
    result, conf = is_question("Сколько это будет стоить?", "ru")
    assert result is True
    assert abs(conf - 0.95) < 0.01


def test_question_without_mark():
    """«сколько это будет стоить» (без знака) / ru → True, ≥ 0.8"""
    result, conf = is_question("сколько это будет стоить", "ru")
    assert result is True
    assert conf >= 0.8


def test_english_wh_word():
    """«What is your delivery time» / en → True"""
    result, conf = is_question("What is your delivery time", "en")
    assert result is True


def test_english_aux_verb():
    """«Can you deliver by March» / en → True (aux verb)"""
    result, conf = is_question("Can you deliver by March", "en")
    assert result is True


def test_spanish_inverted_question():
    """«¿Cuánto cuesta» / es → True"""
    result, conf = is_question("¿Cuánto cuesta", "es")
    assert result is True
    assert abs(conf - 0.95) < 0.01


def test_polish_particle():
    """«Czy możemy zacząć w marcu» / pl → True (частица czy)"""
    result, conf = is_question("Czy możemy zacząć w marcu", "pl")
    assert result is True
    assert conf >= 0.75


def test_long_statement_no_question():
    """«Мы можем начать в марте, если вы подтвердите условия» / ru → False"""
    result, conf = is_question(
        "Мы можем начать в марте, если вы подтвердите условия", "ru"
    )
    assert result is False


def test_short_no_question_at_boundary():
    """«что-то не так» / ru (3 слова — на границе) → False"""
    result, conf = is_question("что-то не так", "ru")
    assert result is False


def test_compound_question():
    """«Сколько стоит? Мы смотрели у конкурентов» / ru → True, 0.85"""
    result, conf = is_question("Сколько стоит? Мы смотрели у конкурентов", "ru")
    assert result is True
    assert abs(conf - 0.85) < 0.01


def test_unknown_language_no_mark():
    """Неизвестный язык xx, «сколько это стоит» → False"""
    result, conf = is_question("сколько это стоит", "xx")
    assert result is False


def test_unknown_language_with_mark():
    """Неизвестный язык xx, «сколько это стоит?» → True (только ?)"""
    result, conf = is_question("сколько это стоит?", "xx")
    assert result is True


def test_empty_string():
    """Пустая строка → (False, 0.0)"""
    result, conf = is_question("", "ru")
    assert result is False
    assert conf == 0.0


def test_whitespace_string():
    """Строка из пробелов → (False, 0.0)"""
    result, conf = is_question("   ", "ru")
    assert result is False
    assert conf == 0.0


def test_score_separate_from_decision():
    """score() возвращает 0..1, не влияет на is_question()"""
    s = score("Сколько это будет стоить?", "ru")
    assert 0 <= s <= 1


def test_mid_sentence_question_word():
    """Вопросительное слово в середине даёт 0.45, при пороге 0.5 → False"""
    result, conf = is_question("Я не знаю как это работает", "ru")
    assert result is False
    assert abs(conf - 0.45) < 0.01


def test_ukrainian_particle():
    """«Чи є питання?» / uk → True"""
    result, conf = is_question("Чи є питання", "uk")
    assert result is True
    assert conf >= 0.75


def test_no_state_between_calls():
    """Два вызова с одним входом дают одинаковый результат — нет состояния"""
    r1, c1 = is_question("Сколько стоит?", "ru")
    r2, c2 = is_question("Сколько стоит?", "ru")
    assert r1 == r2
    assert c1 == c2


def test_long_utterance_penalty():
    """Длинная реплика с ? — уверенность умножена на 0.5"""
    # 61+ слов -> max_words penalty activates
    long_text = "слово " * 65
    result, conf = is_question(long_text, "ru")
    assert conf == 0.0  # nothing triggered after penalty


def test_question_word_at_start_ru_weight():
    """Вопросительное слово в начале — вес 0.80"""
    result, conf = is_question("Когда будет готово", "ru")
    assert result is True
    assert abs(conf - 0.80) < 0.01


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
