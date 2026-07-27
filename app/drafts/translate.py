"""app/drafts/translate.py — I4 draft translator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.translation.base import TranslationProvider, TranslationMode
from app.drafts.guardrails import DraftGuard
from app.errors import InvariantViolation
from app.privacy import Fence


@dataclass(frozen=True, slots=True)
class DraftTranslateConfig:
    mode: TranslationMode = TranslationMode.LIVE_LITERAL  # not changeable
    source_language: str = "ru"  # [draft].generate_language
    reject_on_drift: bool = True


class DraftTranslator:
    def __init__(
        self,
        provider: TranslationProvider,
        guard: DraftGuard,
        config: DraftTranslateConfig | None = None,
    ) -> None:
        # Validate mode at construction time (fail fast)
        if config and config.mode != TranslationMode.LIVE_LITERAL:
            raise InvariantViolation("draft_mode_forbidden")
        self._provider = provider
        self._guard = guard
        self._config = config or DraftTranslateConfig()
        self._snapshot: dict = {
            "translated": 0,
            "rejected_drift": 0,
            "rejected_empty": 0,
            "skipped_same_lang": 0,
        }

    async def translate_draft(
        self, draft_id: str, draft_ru: str, target_language: str,
        *, fence: "Fence",
    ) -> str | None:
        """Translate a draft from Russian to target_language.

        Returns the translated text if it should be attached, or None if
        translation should not be attached (e.g., same language, drift rejected,
        empty translation). The method does NOT perform attachment; that is
        done by DraftGuard.attach_translation.
        """
        # Normalize languages (lowercase, strip region)
        src = self._config.source_language.lower()
        tgt = target_language.lower()
        # Remove any region specifier (e.g., 'ru-ru' -> 'ru')
        if "-" in src:
            src = src.split("-")[0]
        if "-" in tgt:
            tgt = tgt.split("-")[0]

        # If same language, no translation needed
        if src == tgt:
            self._snapshot["skipped_same_lang"] += 1
            return None

        # Build TranslationRequest (context empty, segment_id None, mode LIVE_LITERAL)
        from app.translation.base import TranslationRequest

        req = TranslationRequest(
            text=draft_ru,
            source_language=self._config.source_language,  # keep original for provider
            target_language=target_language,  # keep original for provider
            mode=self._config.mode,  # should be LIVE_LITERAL
            context=(),
            segment_id=None,
        )

        # Translate via provider (which will apply privacy, audit, etc.)
        # fence приходит параметром от вызывающего — он захватил его в момент
        # постановки задачи. Передаём провайдеру как есть.

        try:
            result = await self._provider.translate(req, fence=fence)
        except Exception:
            # If the provider raises an error (e.g., privacy violation, auth error),
            # we should propagate it? The spec says: "Ошибки провайдера пробрасываются как есть."
            # So we let it propagate.
            raise

        # Check for empty translation when input non-empty
        if not result.translation_raw and req.text:
            self._snapshot["rejected_empty"] += 1
            return None

        # Аудит уже прогнан в base.translate, изменения в result.changes — читаем их
        lost = [c for c in result.changes if c.type == "lost_entity"]
        if self._config.reject_on_drift and lost:
            self._snapshot["rejected_drift"] += 1
            return None

        # Translation is acceptable
        self._snapshot["translated"] += 1
        # Return the translation text (raw or cleaned? The spec says attach_translation
        # expects the translated text. The DraftGuard.attach_translation likely expects
        # the translated string. We'll return the translation_raw.
        return result.translation_raw

    def snapshot(self) -> dict:
        """Return counters for debugging/monitoring."""
        return self._snapshot.copy()