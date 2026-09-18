"""app/stt/factory.py — сборка STT-провайдеров по конфигу.

Два независимых артефакта (A/§8.7):
  * `build_stt_cloud_chain` — облачные звенья (hedging, cooldown), исполняются
    ВНЕ сериализованного локального воркера;
  * `build_local_provider` — терминальный локальный фолбэк, ставится
    провайдером `SttScheduler` (один экземпляр whisper).

`local_whisper` не входит в облачную цепочку: это чёрный ящик-фолбэк на случай
закрытого профиля (§приватность) или недоступности облака.
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


def build_stt_cloud_chain(
    stt: SttSection,
    *,
    privacy: Optional[PrivacyController] = None,
    keystore: Optional[KeyStore] = None,
    secrets_dir: Path = DEFAULT_SECRETS_DIR,
) -> Optional[SttFailoverChain]:
    """Облачная цепочка с hedging. None — если облачных звеньев нет."""
    entries = [
        ChainEntry(
            provider=_build_cloud_provider(
                e, privacy=privacy, keystore=keystore, secrets_dir=secrets_dir
            ),
            cooldown_s=e.cooldown_s,
        )
        for e in stt.chain
        if e.provider != "local_whisper"
    ]
    if not entries:
        return None
    return SttFailoverChain(
        entries,
        hedge_delay_s=stt.hedge_delay_s,
        deadline_s=stt.cloud_deadline_s,
    )


def build_local_provider(
    stt: SttSection,
    *,
    binary: Path = DEFAULT_BINARY,
    threads: int = 4,
    models_dir: Path = DEFAULT_MODELS_DIR,
) -> LocalWhisperProvider:
    """Терминальный локальный фолбэк (инвариант §8.7: звено обязано быть)."""
    local = next((e for e in stt.chain if e.provider == "local_whisper"), None)
    if local is None:
        raise ValueError(
            "stt.chain must contain a local_whisper entry (invariant §8.7)"
        )
    return LocalWhisperProvider(
        model_path=resolve_model_path(local.model, models_dir),
        fallback_model_path=resolve_model_path(
            local.fallback_model or local.model, models_dir
        ),
        binary=binary,
        threads=threads,
        device=local.device,
    )


def _build_cloud_provider(
    entry: SttChainEntry,
    *,
    privacy: Optional[PrivacyController],
    keystore: Optional[KeyStore],
    secrets_dir: Path,
) -> CloudSttProvider:
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
