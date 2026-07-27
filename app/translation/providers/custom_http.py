"""app/translation/providers/custom_http.py — D3 custom HTTP provider (OpenAI-compatible)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

from app.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseInvalid,
    ProviderUnavailable,
)
from app.privacy import Fence, PrivacyController
from app.translation.base import (
    BaseTranslationProvider,
    TranslationMode,
    TranslationRequest,
    TranslationResult,
)
from app.translation.prompts import build, validate_response


@dataclass(frozen=True)
class CustomHttpConfig:
    endpoint: str = ""
    model: str = ""
    timeout_s: float = 15.0
    max_tokens: int = 1024
    temperature: float = 0.0


class CustomHttpProvider(BaseTranslationProvider):
    """Generic OpenAI-compatible HTTP provider (e.g., vLLM, Ollama, custom endpoint)."""

    def __init__(
        self,
        privacy: PrivacyController,
        key_provider: Callable[[], str],
        *,
        endpoint: str = "",
        model: str = "",
        timeout_s: float = 15.0,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(
            name="custom_http",
            privacy=privacy,
            key_provider=key_provider,
            timeout_s=timeout_s,
        )
        self._endpoint = endpoint
        self._model = model
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout_s)

    @property
    def privacy(self) -> PrivacyController:
        return self._privacy

    async def _call(self, req: TranslationRequest, api_key: str) -> str:
        system_text, user_text = build(req.mode, req)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }

        if req.mode == TranslationMode.POST_CLEAN:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        url = self._endpoint.rstrip("/") + "/chat/completions"

        try:
            resp = await self._http_client.post(url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise ProviderUnavailable(f"request failed: {exc}") from None

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

        choices = data.get("choices")
        if not choices:
            raise ProviderResponseInvalid("no choices in response")

        choice = choices[0]
        message = choice.get("message")
        if not message:
            raise ProviderResponseInvalid("no message in choice")

        content = message.get("content", "").strip()
        if not content:
            raise ProviderResponseInvalid("empty content")

        return content

    def _parse(self, req: TranslationRequest, raw: str) -> TranslationResult:
        return validate_response(req.mode, raw)

    async def translate(self, req: TranslationRequest, *, fence: Fence) -> TranslationResult:
        return await super().translate(req, fence=fence)

    async def close(self) -> None:
        await self._http_client.aclose()
