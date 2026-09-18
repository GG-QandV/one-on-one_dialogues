"""app/security/keyfiles.py — ключи из локальных файлов (`~/.secrets/<name>`).

Не заменяет `KeyStore`, а наполняет его: секреты не кладутся в config.toml
и не передаются через командную строку (глобальное правило SECRETS). Файл
читается по имени звена (`key_name`), содержимое уходит в `KeyStore`, где
маскируется и регистрируется в LogRedactor.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.security.byok import KeyStore

log = logging.getLogger(__name__)

#: Каталог локальных секретов по умолчанию.
DEFAULT_SECRETS_DIR = Path.home() / ".secrets"


def read_secret_file(
    name: str, secrets_dir: Path = DEFAULT_SECRETS_DIR
) -> str | None:
    """Прочитать секрет по имени. Имя — простой идентификатор (без слэшей)."""
    if not name or "/" in name or name.startswith("."):
        return None
    path = Path(secrets_dir) / name
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("не удалось прочитать секрет %s: %s", name, type(exc).__name__)
        return None
    return value or None


def load_key_file(
    keystore: KeyStore,
    name: str,
    secrets_dir: Path = DEFAULT_SECRETS_DIR,
) -> bool:
    """Загрузить файловый секрет в KeyStore. False — файла нет/пуст."""
    value = read_secret_file(name, secrets_dir)
    if value is None:
        return False
    keystore.put(name, value)
    return True
