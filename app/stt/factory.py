"""app/stt/factory.py — сборка активного STT-провайдера по конфигу.

Выбор — по `config.stt.active`, зеркально `_build_provider()` для перевода.
Одна точка истины: и старт процесса, и `POST /api/stt` идут сюда.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.config import SttSection
from app.privacy import PrivacyController
from app.stt.base import SttProvider
from app.stt.cloud_api import CloudSttProvider
from app.stt.local_whisper import LocalWhisperProvider

#: Каталог моделей whisper.cpp по умолчанию (относительно корня проекта).
DEFAULT_MODELS_DIR = Path("models")
DEFAULT_BINARY = Path("whisper-cli")


def resolve_model_path(model: str, models_dir: Path = DEFAULT_MODELS_DIR) -> Path:
    """Имя модели → путь. Абсолютный/содержащий '/' путь берётся как есть."""
    p = Path(model)
    if p.is_absolute() or "/" in model:
        return p
    return models_dir / model


def build_stt_provider(
    stt: SttSection,
    *,
    privacy: PrivacyController | None = None,
    key_provider: Callable[[], str] | None = None,
    binary: Path = DEFAULT_BINARY,
    threads: int = 4,
    models_dir: Path = DEFAULT_MODELS_DIR,
) -> SttProvider:
    """Построить провайдер по секции `[stt]`."""
    if stt.active == "local_whisper":
        return LocalWhisperProvider(
            model_path=resolve_model_path(stt.local.model, models_dir),
            fallback_model_path=resolve_model_path(
                stt.local.fallback_model, models_dir
            ),
            binary=binary,
            threads=threads,
            device=stt.local.device,
        )
    return CloudSttProvider(
        active=stt.active,
        endpoint=stt.cloud.endpoint,
        model=stt.cloud.model,
        language_hint=stt.cloud.language_hint,
        timeout_s=stt.cloud.timeout_s,
        privacy=privacy,
        key_provider=key_provider,
    )
