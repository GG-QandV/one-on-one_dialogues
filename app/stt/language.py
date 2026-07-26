"""app/stt/language.py — разрешение конфликта заданного и определённого языка. Задача C8 роадмапа."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ------------------------------------------------------------------ data class


@dataclass(frozen=True, slots=True)
class LanguageDecision:
    """Результат разрешения конфликта языков."""
    effective: str          # что уходит в перевод — ВСЕГДА configured, кроме случая configured == "auto"
    configured: str         # как настроено пользователем
    detected: Optional[str] # что распознал whisper (может быть None)
    conflict: bool          # True — есть конфликт, нужно показать индикатор
    note: Optional[str]     # пояснение для пользователя на русском


# ------------------------------------------------------------------ helpers


# Map of language codes to their Russian names for notes.
_NAMES: dict[str, str] = {
    "ru": "русский",
    "en": "английский",
    "es": "испанский",
    "uk": "украинский",
    "pl": "польский",
    "de": "немецкий",
    "fr": "французский",
    "pt": "португальский",
    "it": "итальянский",
}


def _normalize_lang(code: Optional[str]) -> Optional[str]:
    """Нормализовать код языка: нижний регистр, убрать регион, применить синонимы.
    Возвращает None, если вход None или пустая строка.
    """
    if not code:
        return None
    code = code.strip().lower()
    # Remove region part (everything after '-')
    if "-" in code:
        code = code.split("-")[0]
    # Apply synonyms
    synonyms = {
        "iw": "he",
        "in": "id",
        "jw": "jv",
        "ua": "uk",
    }
    return synonyms.get(code, code)


def _get_language_name(code: Optional[str]) -> str:
    """Вернуть название языка на русском по коду, либо сам код если неизвестен."""
    if code is None:
        return ""
    return _NAMES.get(code, code)


# ------------------------------------------------------------------ main function


def resolve(configured: str, detected: Optional[str], autodetect_enabled: bool) -> LanguageDecision:
    """Разрешить конфликт между настроенным и определённым языком.

    Args:
        configured: язык, выбранный пользователем в настройках (строка, не пустая).
        detected: язык, распознанный Whisper (может быть None или пустой строкой).
        autodetect_enabled: включён ли автодетекция языка в Whisper.

    Returns:
        LanguageDecision: результат разрешения.
    """
    # Normalize inputs for comparison
    norm_configured = _normalize_lang(configured)
    norm_detected = _normalize_lang(detected)

    # Determine effective language
    if configured == "auto":
        effective = detected or "auto"
    else:
        effective = configured

    # Initialize conflict and note
    conflict = False
    note: Optional[str] = None

    # Check for conflict conditions
    if autodetect_enabled and detected is not None and detected != "" and configured != "auto":
        if norm_configured != norm_detected:
            conflict = True
            # Check for close language pairs
            close_pairs = [
                ("ru", "uk"),
                ("ru", "be"),
                ("cs", "sk"),
                ("hr", "sr"),
                ("id", "ms"),
            ]
            is_close = False
            for a, b in close_pairs:
                if (norm_configured == a and norm_detected == b) or (norm_configured == b and norm_detected == a):
                    is_close = True
                    break
            if is_close:
                note = (
                    f"Задан {_get_language_name(norm_configured)}, "
                    f"распознан {_get_language_name(norm_detected)}. "
                    f"Близкие языки, детект ненадёжен на коротких репликах."
                )
            else:
                note = (
                    f"Задан {_get_language_name(norm_configured)}, "
                    f"распознан {_get_language_name(norm_detected)}."
                )
        # else: no conflict, already False and None
    # else: no conflict, already False and None

    return LanguageDecision(
        effective=effective,
        configured=configured,
        detected=detected,
        conflict=conflict,
        note=note,
    )