"""app/stt/cloud_api.py — облачный STT-провайдер (OpenAI-совместимый API).

Реализован реальный вызов `POST {endpoint}` c multipart-формой
(`file`, `model`, `language`, `response_format=verbose_json`) и Bearer-ключом —
как у Groq (`https://api.groq.com/openai/v1/audio/transcriptions`,
модели `whisper-large-v3-turbo` / `whisper-large-v3`) и у OpenAI.

Провайдерная обвязка (приватность, ключ, таймаут, классификация HTTP-ошибок)
наследуется из `BaseSttProvider`; ключ в тело ошибки не попадает.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

from app.errors import ProviderResponseInvalid, ProviderUnavailable
from app.privacy import PrivacyController
from app.stt.base import BaseSttProvider, SttRequest, SttResult

#: Дефолтный endpoint OpenAI, если для `openai_api` endpoint не задан явно.
DEFAULT_OPENAI_STT_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"


class CloudSttProvider(BaseSttProvider):
    """Облачное распознавание. Ключ живёт только в KeyStore, не в TOML."""

    def __init__(
        self,
        *,
        active: str,
        endpoint: str,
        model: str,
        timeout_s: float = 15.0,
        privacy: Optional[PrivacyController] = None,
        key_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        super().__init__(
            active, privacy, key_provider, timeout_s=timeout_s  # type: ignore[arg-type]
        )
        self._endpoint = endpoint
        self._model = model

    # ------------------------------------------------------------- HTTP

    def _resolve_url(self) -> str:
        if self._endpoint:
            return self._endpoint
        if self.name == "openai_api":
            return DEFAULT_OPENAI_STT_ENDPOINT
        raise ProviderUnavailable("stt endpoint not configured")

    async def _call(self, req: SttRequest, api_key: str) -> str:
        url = self._resolve_url()
        audio_path = Path(req.audio_path)
        try:
            audio = audio_path.read_bytes()
        except OSError as exc:  # путь не течёт в текст ошибки
            raise ProviderUnavailable(f"audio read failed: {type(exc).__name__}") from None

        form = aiohttp.FormData()
        form.add_field(
            "file", audio,
            filename=audio_path.name or "audio.wav",
            content_type="audio/wav",
        )
        form.add_field("model", self._model)
        form.add_field("response_format", "verbose_json")
        if req.language_hint:
            form.add_field("language", req.language_hint)

        headers = {"Authorization": f"Bearer {api_key}"}
        client_timeout = aiohttp.ClientTimeout(total=self._timeout_s + 5.0)
        async with (
            aiohttp.ClientSession(timeout=client_timeout) as session,
            session.post(url, data=form, headers=headers) as resp,
        ):
            body = await resp.text()
            if resp.status >= 400:
                raise self._classify(resp.status, body)
            return body

    def _parse(self, req: SttRequest, raw: str) -> SttResult:
        _ = req
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderResponseInvalid("invalid JSON from STT provider") from exc
        if not isinstance(data, dict) or "text" not in data:
            raise ProviderResponseInvalid("missing 'text' in STT response")

        return SttResult(
            raw_text=str(data.get("text") or "").strip(),
            detected_language=data.get("language"),
            confidence=_mean_logprob(data.get("segments")),
            model=self._model,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self._model,
            "endpoint": self._endpoint,
            "timeout_s": self._timeout_s,
        }


def _mean_logprob(segments: Any) -> Optional[float]:
    """Средний avg_logprob по сегментам verbose_json (как метрика уверенности)."""
    if not isinstance(segments, list):
        return None
    values = [
        s["avg_logprob"]
        for s in segments
        if isinstance(s, dict) and isinstance(s.get("avg_logprob"), (int, float))
    ]
    if not values:
        return None
    return sum(values) / len(values)
