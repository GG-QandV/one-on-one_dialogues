"""app/drafts/trigger.py — I3 question detection trigger.

Чистая функция: правила, без LLM/ML. Горячий путь — на каждый сегмент.
"""

from __future__ import annotations

import string
from dataclasses import dataclass

QUESTION_WORDS: dict[str, frozenset[str]] = {
    "ru": frozenset({
        "кто", "что", "где", "когда", "почему", "зачем",
        "сколько", "какой", "какая", "какие", "как", "чей",
    }),
    "en": frozenset({
        "who", "what", "where", "when", "why", "how",
        "which", "whose", "whom",
    }),
    "es": frozenset({
        "quién", "qué", "dónde", "cuándo", "por", "cómo",
        "cuánto", "cuál",
    }),
    "uk": frozenset({
        "хто", "що", "де", "коли", "чому", "скільки",
        "який", "як", "чий",
    }),
    "pl": frozenset({
        "kto", "co", "gdzie", "kiedy", "dlaczego", "ile",
        "jaki", "jak", "czyj",
    }),
}

AUX_VERBS: dict[str, frozenset[str]] = {
    "en": frozenset({
        "do", "does", "did", "is", "are", "was", "were",
        "can", "could", "will", "would", "should", "have",
        "has", "may", "might",
    }),
}

PARTICLES: dict[str, tuple[str, ...]] = {
    "ru": ("ли",),
    "uk": ("чи",),
    "pl": ("czy",),
}


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    threshold: float = 0.5
    min_words: int = 3
    max_words: int = 60


CONFIG: TriggerConfig = TriggerConfig()

_LEAD_PUNCT = string.punctuation + "¿¡"


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _tokenize(text: str) -> list[str]:
    return _normalize(text).split()


def _strip_punct(word: str) -> str:
    return word.strip(_LEAD_PUNCT)


def _match_word(word: str, words: frozenset[str]) -> bool:
    return _strip_punct(word) in words


def _first_n_match(tokens: list[str], words: frozenset[str], n: int) -> bool:
    for t in tokens[:n]:
        if _match_word(t, words):
            return True
    return False


def score(text: str, language: str) -> float:
    """Только уверенность 0..1, без решения. Для отладочного экрана E6."""
    if not text or not text.strip():
        return 0.0

    text = _normalize(text)
    tokens = _tokenize(text)
    word_count = len(tokens)
    confidence = 0.0

    # Пунктуационные сигналы — работают всегда, независимо от языка и длины
    if language == "es" and text.startswith("¿"):
        confidence = 0.95
    if text.endswith("?"):
        confidence = max(confidence, 0.95)
    elif "?" in text:
        # Составная реплика: ? внутри, но не в конце
        confidence = max(confidence, 0.85)

    # Словарные сигналы — только если достаточно слов и язык известен
    if word_count >= CONFIG.min_words and language in QUESTION_WORDS:
        q_words = QUESTION_WORDS[language]
        aux_words = AUX_VERBS.get(language, frozenset())
        particles = PARTICLES.get(language, ())

        # Вопросительное слово в первых двух токенах
        if _first_n_match(tokens, q_words, 2):
            confidence = max(confidence, 0.80)

        # Вспомогательный глагол в начале (EN)
        if tokens and _match_word(tokens[0], aux_words):
            confidence = max(confidence, 0.70)

        # Вопросительная частица в любом токене
        for p in particles:
            if any(p == _strip_punct(t) for t in tokens):
                confidence = max(confidence, 0.75)
                break

        # Вопросительное слово НЕ в первых двух токенах (середина фразы)
        if len(tokens) > 2 and _first_n_match(tokens[2:], q_words, len(tokens) - 2):
            confidence = max(confidence, 0.45)

    # Длинные реплики — обычно монолог, умножаем
    if word_count > CONFIG.max_words:
        confidence *= 0.5

    return confidence


def is_question(text: str, language: str) -> tuple[bool, float]:
    """-> (решение, уверенность 0..1). Порог из TriggerConfig."""
    c = score(text, language)
    return c >= CONFIG.threshold, c
