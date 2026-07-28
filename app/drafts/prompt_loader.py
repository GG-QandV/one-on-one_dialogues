"""app/drafts/prompt_loader.py — загрузка DRAFT-системного промпта из конфига.

§12 спеки НЕ описывает этот текст дословно (осознанное отклонение): промпт
живёт внешним файлом и подставляет язык/тон из панели. Hot-reload по mtime —
как остальной конфиг проекта, без рестарта.

Плейсхолдеры в шаблоне: {generate_language}, {tone_preset}, {tone_note}.
Блок СПРАВКА в промпт НЕ входит — библиотека вставляется провайдером как
кэш-префикс отдельно (см. provider.py). Этот промпт — только system-часть.
"""

from __future__ import annotations

import threading
from pathlib import Path


class DraftPromptTemplate:
    """Шаблон DRAFT-промпта с ленивой перезагрузкой по mtime."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._cached_text: str | None = None
        self._cached_mtime: float = -1.0

    def _load_if_changed(self) -> str:
        with self._lock:
            try:
                mtime = self._path.stat().st_mtime
            except OSError as exc:
                if self._cached_text is not None:
                    return self._cached_text          # файл исчез — держим кэш
                raise FileNotFoundError(
                    f"DRAFT-промпт не найден: {self._path}"
                ) from exc
            if mtime != self._cached_mtime or self._cached_text is None:
                self._cached_text = self._path.read_text(encoding="utf-8")
                self._cached_mtime = mtime
            return self._cached_text

    def build_system(
        self,
        generate_language: str,
        tone_preset: str = "neutral",
        tone_note: str = "",
    ) -> str:
        """Собрать системный промпт с подстановкой языка и тона.

        Плейсхолдеры, которых нет в шаблоне, игнорируются; лишние фигурные
        скобки в теле промпта не ломают подстановку (используется явный
        replace, не str.format — в промпте есть JSON с {}).
        """
        tmpl = self._load_if_changed()
        return (
            tmpl
            .replace("{generate_language}", generate_language or "en")
            .replace("{tone_preset}", tone_preset or "neutral")
            .replace("{tone_note}", tone_note or "")
        )
