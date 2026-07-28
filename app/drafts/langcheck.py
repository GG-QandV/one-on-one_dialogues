"""app/drafts/langcheck.py — грубая проверка языка черновика.

Задача узкая: убедиться, что сгенерированный черновик написан на ЗАДАННОМ
языке, и поймать грубый промах (модель ответила не на том языке). Точная
классификация не нужна — нужна дешёвая и быстрая отбраковка.

Два уровня, оба без внешних зависимостей:
  1. СИСТЕМА ПИСЬМА (мгновенно, по Unicode-диапазонам):
     кириллица / латиница / CJK. Ловит «нужен ru → пришёл en»,
     «иероглифы там, где не ждём».
  2. ЯЗЫК внутри одной письменности (компактный n-gram + буквы-маркеры):
     различает en / es / pl (все латиница) и ru / uk (обе кириллица).

Поддерживаемые языки проекта: ru, uk, en, es, pl. Для языка вне списка
проверка уровня 2 пропускается (только скрипт).

ГРАНИЦА МЕТОДА (осознанная): ru↔uk различаются только при наличии
буквы-маркера (ru: ы/э/ъ/ё; uk: і/ї/є/ґ). Кириллическая фраза без таких
букв считается прошедшей для любого из этих двух языков — грубый детектор
её не разводит. Промах письменности (кириллица↔латиница↔CJK) и en↔es
ловятся надёжно. Для цели «грубо и быстро» это приемлемо; тонкий ru↔uk
добивается усиленной инструкцией на retry, не детектором.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LangVerdict:
    ok: bool
    detected: str        # "ru" | "en" | ... | "script:cyrillic" | "unknown"
    reason: str


# --------------------------------------------------------------- письменность

_CYR = re.compile(r"[\u0400-\u04FF]")
_LAT = re.compile(r"[A-Za-z\u00C0-\u024F]")   # латиница + расширения (á, ñ, ł …)
_CJK = re.compile(r"[\u3000-\u9FFF\uF900-\uFAFF]")

_SCRIPT_OF = {
    "ru": "cyrillic", "uk": "cyrillic",
    "en": "latin", "es": "latin", "pl": "latin",
}


def _dominant_script(text: str) -> str:
    cyr = len(_CYR.findall(text))
    lat = len(_LAT.findall(text))
    cjk = len(_CJK.findall(text))
    total = cyr + lat + cjk
    if total == 0:
        return "unknown"
    if cjk and cjk / total > 0.15:   # иероглифы даже в меньшинстве — подозрительно
        return "cjk"
    if cyr >= lat:
        return "cyrillic"
    return "latin"


# ---------------------------------------------------------- буквы-маркеры (L2)

# Символы, надёжно указывающие на конкретный язык внутри письменности.
_LETTER_MARKERS = {
    "uk": set("іїєґ"),           # укр. буквы, которых нет в рус. алфавите
    "ru": set("ыэъё"),           # рус. буквы, которых нет в укр.
    "pl": set("łżźćśńąę"),       # характерные польские
    "es": set("ñ¿¡"),            # характерные испанские
}


# ------------------------------------------------- частотные слова/триграммы (L2)

# Короткие частотные признаки. Не полноценный корпус — грубый различитель.
_WORD_MARKERS = {
    "en": {"the", "and", "is", "to", "of", "for", "with", "that", "will", "can"},
    "es": {"el", "la", "de", "que", "los", "las", "una", "por", "para", "con"},
    "pl": {"i", "w", "na", "nie", "jest", "do", "to", "się", "że", "z"},
    "ru": {"и", "в", "не", "на", "что", "с", "это", "для", "по", "как"},
    "uk": {"і", "в", "не", "на", "що", "з", "це", "для", "по", "як"},
}

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).lower()


def _letter_score(text: str) -> dict[str, int]:
    chars = set(text)
    return {lang: len(chars & marks) for lang, marks in _LETTER_MARKERS.items()}


def _word_score(text: str) -> dict[str, int]:
    words = set(_WORD_RE.findall(text))
    return {lang: len(words & marks) for lang, marks in _WORD_MARKERS.items()}


def _detect_within_script(text: str, script: str) -> str:
    """Определить конкретный язык внутри письменности. Возвращает код или ''. """
    candidates = [l for l, s in _SCRIPT_OF.items() if s == script]
    if len(candidates) <= 1:
        return candidates[0] if candidates else ""

    letters = _letter_score(text)
    words = _word_score(text)
    # суммарный балл: буквы-маркеры весомее (жёсткий признак)
    best, best_score = "", -1
    for lang in candidates:
        score = letters.get(lang, 0) * 3 + words.get(lang, 0)
        if score > best_score:
            best, best_score = lang, score
    return best if best_score > 0 else ""   # '' = не различили


# --------------------------------------------------------------- публичный API

def check_language(text: str, expected: str) -> LangVerdict:
    """Грубая проверка: написан ли text на языке expected.

    ok=True — язык совпал ИЛИ различить внутри письменности не удалось, но
    письменность верная (не штрафуем за неуверенность L2). ok=False —
    грубый промах: чужая письменность или уверенно другой язык.
    """
    body = _normalize(text)
    if not body.strip():
        return LangVerdict(True, "unknown", "пустой текст — не проверяем")

    expected = (expected or "").lower()
    exp_script = _SCRIPT_OF.get(expected)

    dom = _dominant_script(body)

    # Язык вне списка проекта — проверяем только что текст непустой и не CJK-мусор.
    if exp_script is None:
        if dom == "cjk":
            return LangVerdict(False, "cjk", "иероглифы при неожидаемом CJK")
        return LangVerdict(True, dom, "язык вне списка — только скрипт-проверка")

    # Уровень 1: письменность.
    if dom == "cjk":
        return LangVerdict(False, "cjk",
                           "иероглифы в теле — грубый промах языка")
    if dom != "unknown" and dom != exp_script:
        return LangVerdict(False, f"script:{dom}",
                           f"письменность {dom}, ожидалась {exp_script}")

    # Уровень 2: язык внутри письменности.
    detected = _detect_within_script(body, exp_script)
    if not detected:
        # не различили — не штрафуем, письменность верная
        return LangVerdict(True, f"script:{exp_script}",
                           "письменность верна, язык не различён — принято")
    if detected != expected:
        return LangVerdict(False, detected,
                           f"определён {detected}, ожидался {expected}")
    return LangVerdict(True, detected, "язык совпал")
