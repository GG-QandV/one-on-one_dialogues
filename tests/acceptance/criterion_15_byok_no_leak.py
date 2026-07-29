"""Критерий 15 (§21): BYOK-ключ не оказывается на диске и не в логах.

Грep по артефактам, а не по коду (H2, пункт 4): канареечный ключ кладётся
в KeyStore, гоняется через логгер с подключённым LogRedactor (симуляция
ошибки провайдера — именно так ключ обычно утекает в трейсы), затем ищем
строку буквально во ВСЕХ файлах временного окружения — включая *.db-wal
и *.db-shm (WAL-режим держит данные там до checkpoint).
"""

from __future__ import annotations

import logging
import uuid

from app.security.byok import KeyStore
from app.security.redactor import LogRedactor
from tests.acceptance.harness import CANARY_KEY, CheckDef, CheckEnv, CheckKind, CheckResult


async def _run(env: CheckEnv) -> CheckResult:
    redactor = LogRedactor()
    keystore = KeyStore(redactor=redactor)
    keystore.put("canary-provider", CANARY_KEY)

    log_path = env.artifacts_dir / "app.log"
    logger = logging.getLogger(f"h2.criterion15.{uuid.uuid4().hex}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.addFilter(redactor)
    logger.addHandler(handler)
    try:
        # Симулируем именно то, из-за чего ключи реально утекают: ошибку
        # авторизации провайдера с ключом в сообщении/трейсе, а не штатный лог.
        logger.error("провайдер canary-provider вернул 401, ключ=%s", CANARY_KEY)
        logger.error("исходящий заголовок: Authorization: Bearer %s", CANARY_KEY)
        try:
            raise RuntimeError(f"auth failed for key {CANARY_KEY}")
        except RuntimeError:
            logger.exception("необработанная ошибка провайдера")
    finally:
        handler.close()
        logger.removeHandler(handler)

    # БД тоже должна остаться в окружении на диске (включая WAL/SHM) —
    # ключ туда попасть не должен ни при каких условиях.
    await env.db.execute(
        "INSERT INTO library_contexts (id, name, domain, content_text, token_estimate, updated_at) "
        "VALUES (?, 'заметка', NULL, 'обычный текст без секретов', 3, '2026-01-01T00:00:00Z')",
        (uuid.uuid4().hex,),
    )

    canary_bytes = CANARY_KEY.encode("utf-8")
    leaked_files = []
    for path in env.tmp_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if canary_bytes in data:
            leaked_files.append(str(path.relative_to(env.tmp_dir)))

    if leaked_files:
        return CheckResult(
            15,
            "BYOK-ключ не на диске и не в логах",
            CheckKind.AUTO,
            False,
            f"канарейка найдена в файлах: {leaked_files}",
        )

    return CheckResult(
        15,
        "BYOK-ключ не на диске и не в логах",
        CheckKind.AUTO,
        True,
        "канарейка отсутствует во всех файлах окружения (лог, БД, WAL/SHM) "
        "после симуляции ошибки провайдера с ключом в сообщении и трейсе",
    )


CHECK = CheckDef(
    number=15, title="BYOK-ключ не на диске и не в логах", kind=CheckKind.AUTO, run=_run
)
