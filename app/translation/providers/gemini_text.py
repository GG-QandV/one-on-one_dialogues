"""app/translation/providers/gemini_text.py — D2 Gemini text provider."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional

import httpx

from app.translation.base import (
    BaseTranslationProvider,
    TranslationRequest,
    TranslationResult,
    TranslationMode,
)
from app.translation.prompts import build, validate_response
from app.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderResponseInvalid,
    ProviderError,
)
from app.privacy import Capability, PrivacyController


@dataclass(frozen=True)
class GeminiConfig:
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta"
    model: str = "gemini-flash-lite-latest"
    timeout_s: float = 15.0
    max_output_tokens: int = 1024
    temperature: float = 0.0  # translation is not creative


class GeminiTextProvider(BaseTranslationProvider):
    def __init__(
        self,
        privacy: PrivacyController,
        key_provider: Callable[[], str],
        *,
        endpoint: str = "https://generativelanguage.googleapis.com/v1beta",
        model: str = "gemini-flash-lite-latest",
        timeout_s: float = 15.0,
        max_output_tokens: int = 1024,
        temperature: float = 0.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        # Initialize base class with name
        super().__init__(
            name="gemini",
            privacy=privacy,
            key_provider=key_provider,
            timeout_s=timeout_s,
            auditor=None,  # Gemini provider does not use auditor; auditing is done in base.translate
        )
        self._endpoint = endpoint
        self._model = model
        self._timeout_s = timeout_s
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout_s)
        self._last_response: Optional[httpx.Response] = None  # for extracting request_id

    @property
    def privacy(self) -> PrivacyController:
        return self._privacy

    async def _call(self, req: TranslationRequest, api_key: str) -> str:
        # Build prompt via D4
        system_text, user_text = build(req.mode, req)

        # Prepare request payload
        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system_text}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "temperature": self._temperature,
                "candidateCount": 1,
                "maxOutputTokens": self._max_output_tokens,
            },
        }

        # For POST_CLEAN, we need JSON output mode
        if req.mode == TranslationMode.POST_CLEAN:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = {
                "type": "OBJECT",
                "properties": {
                    "clean_text": {"type": "STRING"},
                    "changes": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "type": {"type": "STRING"},
                                "original": {"type": "STRING"},
                                "replacement": {"type": "STRING"},
                            },
                        },
                    },
                },
            }

        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }

        url = f"{self._endpoint}/models/{self._model}:generateContent"

        try:
            resp = await self._http_client.post(url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise ProviderUnavailable(f"request failed: {exc}") from None

        # Store response for later use in translate method to extract request_id
        self._last_response = resp

        if resp.status_code == 401 or resp.status_code == 403:
            raise ProviderAuthError(f"auth failed: {resp.status_code}")
        if resp.status_code == 429:
            raise ProviderRateLimited(f"rate limited: {resp.status_code}")
        if 500 <= resp.status_code < 600:
            raise ProviderUnavailable(f"server error: {resp.status_code}")
        if resp.status_code not in (200, 201):
            raise ProviderResponseInvalid(f"unexpected status {resp.status_code}")

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderResponseInvalid(f"invalid JSON: {exc}") from None

        # Блокировка фильтром безопасности: 200 с promptFeedback.blockReason
        prompt_feedback = data.get("promptFeedback") or {}
        block_reason = prompt_feedback.get("blockReason")
        if block_reason:
            raise ProviderResponseInvalid(f"blocked: {block_reason}")

        # Extract text per Gemini spec
        candidates = data.get("candidates")
        if not candidates:
            raise ProviderResponseInvalid("no candidates in response")
        candidate = candidates[0]

        # finishReason != STOP → ProviderResponseInvalid
        finish_reason = candidate.get("finishReason")
        if finish_reason and finish_reason != "STOP":
            raise ProviderResponseInvalid(f"finish_reason: {finish_reason}")
        content = candidate.get("content")
        if not content:
            raise ProviderResponseInvalid("no content in candidate")
        parts = content.get("parts")
        if not parts:
            raise ProviderResponseInvalid("no parts in content")
        # Concatenate all text parts
        text_parts = []
        for part in parts:
            if "text" in part:
                text_parts.append(part["text"])
            # ignore other part types (e.g., inlineData)
        raw_text = "".join(text_parts).strip()

        return raw_text

    def _parse(self, req: TranslationRequest, raw: str) -> TranslationResult:
        # Delegate to D4's validate_response which returns a TranslationResult
        # We need to pass along any request_id we captured.
        base_result = validate_response(req.mode, raw)
        request_id = None
        resp = self._last_response
        self._last_response = None  # reset for next call
        if resp is not None:
            # Try to get from header
            request_id = resp.headers.get("x-request-id")
            if not request_id:
                # Try to get from JSON body
                try:
                    data = resp.json()
                    request_id = data.get("responseId")
                except (json.JSONDecodeError, AttributeError):
                    pass

        if request_id is not None:
            return TranslationResult(
                translation_raw=base_result.translation_raw,
                translation_clean=base_result.translation_clean,
                changes=base_result.changes,
                provider_request_id=request_id,
            )
        else:
            return base_result

    async def translate(self, req: TranslationRequest, *, fence: "Fence") -> "TranslationResult":
        # Delegate to base class translate which handles privacy, key, timeout, fence validation, and auditor.
        return await super().translate(req, fence=fence)

    async def close(self) -> None:
        await self._http_client.aclose()