"""app/drafts/translate.py — I4 draft translator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.translation.base import TranslationProvider, TranslationMode
from app.drafts.guardrails import DraftGuard
from app.translation.prompts import detect_drift
from app.errors import InvariantViolation


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
        self, draft_id: str, draft_ru: str, target_language: str
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
        # We need to provide a fence; but the provider's translate method requires a fence.
        # However, the DraftTranslator does not have a PrivacyController.
        # According to the spec, the provider handles privacy; we must obtain a fence
        # from somewhere. The draft translator likely does not need to enforce privacy
        # because the provider will do it when called from the job worker (which has
        # access to privacy). But the method signature of provider.translate requires a fence.
        # We need to get a fence from the provider's privacy controller? Actually, the
        # provider's translate method expects a fence argument. We can create a dummy fence?
        # No, the fence must come from the PrivacyController via require().
        # Since we don't have a privacy controller here, we must assume that the caller
        # (the job worker) will provide the fence? But the method is called by the job
        # worker, which should have access to the privacy controller via the provider.
        # Let's look at how other translators are used: in base.translate, the fence is
        # passed from the caller. The caller (e.g., the job) must have a privacy controller
        # to call require().
        # However, the DraftTranslator is a helper that is called by the job worker as well.
        # The job worker likely has a privacy controller. But we don't have access to it.
        # Alternative: we can call provider.translate without a fence? Not allowed.
        # We need to change the design: perhaps the DraftTranslator should not call
        # provider.translate directly, but instead call the provider's _call and _parse
        # and handle privacy ourselves? That would duplicate code.
        # Let's check the actual usage: In the spec, the DraftTranslator is used by the
        # DraftGuard? Actually, the DraftTranslator is a separate module that is used by
        # the job worker for draft translation. The job worker likely has a privacy
        # controller and can pass it in? But the DraftTranslator constructor does not
        # take a privacy controller.
        # Looking at the contract for DraftTranslator, it only takes provider, guard, config.
        # The provider is a TranslationProvider, which already has a privacy controller
        # (as a property). So we can get the privacy from the provider: provider.privacy.
        # Then we can call provider.privacy.require(...) to get a fence.
        # However, the provider's translate method already does the privacy check and
        # fence handling. If we call provider.translate, we need to pass a fence.
        # We can obtain a fence by calling provider.privacy.require(Capability.TEXT_TO_CLOUD).
        # But note: the provider's translate method will again call require and validate,
        # which would double-check. That's okay; we can just call require and then
        # call a lower-level method that does not do privacy? But the provider does not
        # expose such a method.
        # Alternatively, we can let the provider's translate handle privacy, and we just
        # need to provide a fence. We can get a fresh fence from the provider's privacy
        # controller each time. That is acceptable because the require() method is
        # idempotent in the sense that it returns a fence for the current generation.
        # However, if we call require and then later the provider calls require again,
        # the generation should be the same if no switch happened in between.
        # We'll do that.

        from app.privacy import Capability

        # Obtain a fence for TEXT_TO_CLOUD
        fence = self._provider.privacy.require(Capability.TEXT_TO_CLOUD)

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

        # Check for drift
        drift = detect_drift(req, result.translation_raw)
        if self._config.reject_on_drift and drift:
            # Any lost_entity means drift
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