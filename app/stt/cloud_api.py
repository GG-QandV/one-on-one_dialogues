"""app/stt/cloud_api.py — облачный STT-провайдер.

Провайдерная обвязка (приватность, ключ через `KeyStore` с именем
`"stt_cloud"`, таймаут, классификация HTTP-ошибок) наследуется из
`BaseSttProvider`. Сам сетевой вызов конкретного API
(`/v1/audio/transcriptions` и совместимые) — отдельная задача уровня D2/D3
(см. REFACTOR_STT_provider_architecture.md, «Не входит в объём»).

До её реализации `_call` поднимает `NotImplementedError`: вызывающий
(`_on_stt_error`) трактует это как постоянную ошибку и НЕ ставит задачу
в очередь повторно, чтобы не зациклить обработку.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.privacy import PrivacyController
from app.stt.base import BaseSttProvider, SttRequest, SttResult


class CloudSttProvider(BaseSttProvider):
    """Облачное распознавание. Ключ живёт только в KeyStore, не в TOML."""

    def __init__(
        self,
        *,
        active: str,
        endpoint: str,
        model: str,
        language_hint: str = "",
        timeout_s: float = 15.0,
        privacy: Optional[PrivacyController] = None,
        key_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        super().__init__(
            active, privacy, key_provider, timeout_s=timeout_s  # type: ignore[arg-type]
        )
        self._endpoint = endpoint
        self._model = model
        self._language_hint = language_hint

    async def _call(self, req: SttRequest, api_key: str) -> str:
        # Аудио и ключ уйдут в POST /v1/audio/transcriptions (D2/D3).
        _ = (req, api_key)
        raise NotImplementedError(
            f"облачный STT API ({self.name}) не реализован — задача D2/D3"
        )

    def _parse(self, req: SttRequest, raw: str) -> SttResult:  # pragma: no cover
        raise NotImplementedError

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self._model,
            "endpoint": self._endpoint,
            "timeout_s": self._timeout_s,
        }
