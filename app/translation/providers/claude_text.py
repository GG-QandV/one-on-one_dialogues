"""app/translation/providers/claude_text.py — D3 Claude text provider."""

from __future__ import annotations

import json
from dataclasses import dataclass
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
from app.privacy import Capability, PrivacyController, Fence


@dataclass(frozen=True)
class ClaudeConfig:
    endpoint: str = "https://api.anthropic.com/v1"
    model: str = "claude-haiku-4-5"  # from config, not hardcoded
    api_version: str = "2023-06-01"
    timeout_s: float = 15.0
    max_tokens: int = 1024
    temperature: float = 0.0  # translation is not creative


class ClaudeTextProvider(BaseTranslationProvider):
    def __init__(
        self,
        privacy: PrivacyController,
        key_provider: Callable[[], str],
        *,
        config: Optional[ClaudeConfig] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        # Initialize base class with name
        super().__init__(
            name="claude",
            privacy=privacy,
            key_provider=key_provider,
            timeout_s=config.timeout_s if config else 15.0,
            auditor=None,  # Claude provider does not use auditor; auditing is done in base.translate
        )
        self._config = config or ClaudeConfig()
        self._http_client = http_client or httpx.AsyncClient(timeout=self._timeout_s)
        self._last_response: Optional[httpx.Response] = None  # for extracting request_id

    @property
    def privacy(self) -> PrivacyController:
        return self._privacy

    async def _call(self, req: TranslationRequest, api_key: str) -> str:
        # Build prompt via D4
        system_text, user_text = build(req.mode, req)

        # Prepare request payload per Claude Messages API
        payload: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "temperature": self._config.temperature,
            "system": system_text,
            "messages": [
                {
                    "role": "user",
                    "content": user_text,
                }
            ],
        }

        # For POST_CLEAN, we need to prefill assistant with '{' to force JSON output
        if req.mode == TranslationMode.POST_CLEAN:
            # According to spec, we add an assistant message with content "{"
            # so that the model continues the JSON object.
            payload["messages"].append(
                {"role": "assistant", "content": "{"}
            )

        # Context handling: according to spec, context is passed as a separate text block
        # before the user text, marked as not to translate.
        # However, the prompts.build function from D4 should already incorporate context
        # into the system and user prompts appropriately.
        # We trust that D4's build handles context; we just use its output.
        # If needed, we could adjust here, but we follow the spec: use prompts.build.

        headers = {
            "x-api-key": api_key,
            "anthropic-version": self._config.api_version,
            "Content-Type": "application/json",
        }

        url = f"{self._config.endpoint}/messages"

        try:
            resp = await self._http_client.post(url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise ProviderUnavailable(f"request failed: {exc}") from None

        # Store response for later use in translate method to extract request_id
        self._last_response = resp

        # Use base _classify to convert status code to appropriate exception
        if resp.status_code not in (200, 201):
            exc = self._classify(resp.status_code, resp.text)
            raise exc

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderResponseInvalid(f"invalid JSON: {exc}") from None

        # stop_reason == "max_tokens" → обрезанный ответ
        stop_reason = data.get("stop_reason")
        if stop_reason == "max_tokens":
            raise ProviderResponseInvalid("stop_reason: max_tokens")

        # Extract text from Claude response
        content = data.get("content")
        if not content or not isinstance(content, list):
            raise ProviderResponseInvalid("no content or content not a list")
        # Concatenate all text blocks
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        raw_text = "".join(text_parts).strip()

        # Для POST_CLEAN предзаполнили ассистента '{', ответ приходит без неё — вернуть
        if req.mode == TranslationMode.POST_CLEAN:
            if not raw_text.startswith("{"):
                raw_text = "{" + raw_text

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
            request_id = resp.headers.get("request-id")
            if not request_id:
                # Try to get from JSON body
                try:
                    data = resp.json()
                    request_id = data.get("id")
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