"""Критерий 7 (§21): переключение профиля рвёт аудио-сессию в течение
500 мс; в конфиденциальном профиле аудио-трафика нет вовсе.

Перехват на уровне транспорта (H2, пункт 3): считаются фактически
отправленные байты через WireTap (см. tests/test_privacy_isolation.py),
а не факт вызова метода close(). Переиспользуем тот же перехватчик и
провайдер — критерий проверяет системный контракт, а не пишет его заново.
"""

from __future__ import annotations

import asyncio
import time

from app.privacy import PrivacyController, PrivacyProfile
from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult
from tests.test_privacy_isolation import WireTap, make_session


async def _run(_env: CheckEnv) -> CheckResult:
    privacy = PrivacyController(PrivacyProfile.OPEN)
    tap = WireTap()
    session = make_session(privacy, tap)
    await session.open()

    stop_pushing = asyncio.Event()

    async def pusher() -> None:
        from app.errors import StaleGenerationError

        while not stop_pushing.is_set():
            try:
                await session.push(b"\x00" * 320)
            except StaleGenerationError:
                return
            await asyncio.sleep(0.005)

    push_task = asyncio.create_task(pusher())
    await asyncio.sleep(0.05)
    if not tap.audio_frames():
        stop_pushing.set()
        await push_task
        return CheckResult(
            7,
            "privacy switch рвёт аудио ≤500мс",
            CheckKind.AUTO,
            False,
            "аудио не пошло до переключения — проверка слепа",
        )

    switch_at = time.monotonic()
    teardown_ms = await privacy.switch(PrivacyProfile.CONFIDENTIAL)
    stop_pushing.set()
    await push_task
    await asyncio.sleep(0.1)

    leaked = tap.audio_after(switch_at)
    closed = tap.closed or tap.aborted

    if leaked:
        return CheckResult(
            7,
            "privacy switch рвёт аудио ≤500мс",
            CheckKind.AUTO,
            False,
            f"утечка {len(leaked)} кадров после switch()",
        )
    if teardown_ms > 500:
        return CheckResult(
            7,
            "privacy switch рвёт аудио ≤500мс",
            CheckKind.AUTO,
            False,
            f"teardown {teardown_ms:.1f} мс > дедлайна 500 мс",
        )
    if not closed:
        return CheckResult(
            7,
            "privacy switch рвёт аудио ≤500мс",
            CheckKind.AUTO,
            False,
            "транспорт не закрыт после switch()",
        )

    return CheckResult(
        7,
        "privacy switch рвёт аудио ≤500мс",
        CheckKind.AUTO,
        True,
        f"teardown {teardown_ms:.1f} мс, утечек 0, транспорт закрыт",
    )


CHECK = CheckDef(
    number=7, title="переключение профиля рвёт аудио-сессию", kind=CheckKind.AUTO, run=_run
)
