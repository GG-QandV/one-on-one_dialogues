"""app/stt/factory.py — сборка цепочки STT-провайдеров по конфигу.

Одна точка истины: и старт процесса, и `POST /api/stt` идут сюда.
Порядок звеньев — как в `config.stt.chain` (инвариант: последнее — local).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.config import SttChainEntry, SttSection
from app.errors import ProviderAuthError
from app.privacy import PrivacyController
from app.security.byok import KeyStore
from app.security.keyfiles import DEFAULT_SECRETS_DIR, load_key_file
from app.stt.chain import ChainEntry, SttFailoverChain
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


def build_stt_chain(
    stt: SttSection,
    *,
    privacy: Optional[PrivacyController] = None,
    keystore: Optional[KeyStore] = None,
    binary: Path = DEFAULT_BINARY,
    threads: int = 4,
    models_dir: Path = DEFAULT_MODELS_DIR,
    secrets_dir: Path = DEFAULT_SECRETS_DIR,
) -> SttFailoverChain:
    """Построить цепочку фолбэков по секции `[stt]`."""
    entries = [
        ChainEntry(
            provider=_build_entry_provider(
                e,
                privacy=privacy,
                keystore=keystore,
                binary=binary,
                threads=threads,
                models_dir=models_dir,
                secrets_dir=secrets_dir,
            ),
            cooldown_s=e.cooldown_s,
        )
        for e in stt.chain
    ]
    return SttFailoverChain(entries)


def _build_entry_provider(
    entry: SttChainEntry,
    *,
    privacy: Optional[PrivacyController],
    keystore: Optional[KeyStore],
    binary: Path,
    threads: int,
    models_dir: Path,
    secrets_dir: Path,
):
    if entry.provider == "local_whisper":
        return LocalWhisperProvider(
            model_path=resolve_model_path(entry.model, models_dir),
            fallback_model_path=resolve_model_path(
                entry.fallback_model or entry.model, models_dir
            ),
            binary=binary,
            threads=threads,
            device=entry.device,
        )

    key_name = entry.key_name

    def key_provider() -> str:
        if keystore is None:
            raise ProviderAuthError(f"keystore unavailable for key '{key_name}'")
        try:
            return keystore.get(key_name)
        except ProviderAuthError:
            # Ключ из файла ~/.secrets/<key_name>: TTL KeyStore (60 мин) истёк
            # или ключ ещё не загружен — перечитываем файл и пробуем снова.
            if load_key_file(keystore, key_name, secrets_dir):
                return keystore.get(key_name)
            raise

    return CloudSttProvider(
        active=entry.provider,
        endpoint=entry.endpoint,
        model=entry.model,
        timeout_s=entry.timeout_s,
        key_name=key_name,
        privacy=privacy,
        key_provider=key_provider,
    )
