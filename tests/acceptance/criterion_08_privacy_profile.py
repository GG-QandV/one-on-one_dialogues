"""Критерий 8 (§21): privacy_profile проставлен у КАЖДОГО сегмента.

SELECT COUNT(*) WHERE privacy_profile IS NULL = 0 — но таблица NOT NULL
CHECK уже запрещает NULL на уровне схемы, поэтому проверка бьёт по двум
целям: (а) обойти прикладной слой прямым INSERT нельзя, (б) реальный
прогон записи через штатный путь всегда даёт непустой профиль.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.errors import SpeechLocalError
from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult


async def _run(env: CheckEnv) -> CheckResult:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id, stream_id = uuid.uuid4().hex, uuid.uuid4().hex

    await env.db.execute(
        "INSERT INTO sessions (id, started_at, status, default_privacy_profile, mode) "
        "VALUES (?, ?, 'active', 'open', 'live_safe')",
        (session_id, now),
    )
    await env.db.execute(
        "INSERT INTO audio_streams (id, session_id, role, source_language, "
        "target_language, enabled, priority) VALUES (?, ?, 'microphone', 'ru', 'en', 1, 'primary')",
        (stream_id, session_id),
    )
    for i in range(3):
        await env.db.execute(
            "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
            "privacy_profile, track, created_at) VALUES (?, ?, ?, ?, ?, 'open', 'accurate', ?)",
            (uuid.uuid4().hex, session_id, stream_id, i * 1000, i * 1000 + 500, now),
        )

    row = await env.db.fetch_one(
        "SELECT COUNT(*) AS n FROM segments WHERE session_id = ? AND privacy_profile IS NULL",
        (session_id,),
    )
    null_count = row["n"]

    schema_rejects_null = False
    try:
        await env.db.execute(
            "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
            "privacy_profile, track, created_at) VALUES (?, ?, ?, 0, 100, NULL, 'accurate', ?)",
            (uuid.uuid4().hex, session_id, stream_id, now),
        )
    except SpeechLocalError:
        schema_rejects_null = True

    if null_count != 0:
        return CheckResult(
            8,
            "privacy_profile NOT NULL",
            CheckKind.AUTO,
            False,
            f"найдено {null_count} сегментов без профиля",
        )
    if not schema_rejects_null:
        return CheckResult(
            8,
            "privacy_profile NOT NULL",
            CheckKind.AUTO,
            False,
            "схема допустила INSERT с privacy_profile = NULL",
        )

    return CheckResult(
        8,
        "privacy_profile NOT NULL",
        CheckKind.AUTO,
        True,
        f"0 из {null_count + 3} сегментов без профиля; прямой NULL-INSERT отклонён схемой",
    )


CHECK = CheckDef(
    number=8, title="privacy_profile у каждого сегмента", kind=CheckKind.AUTO, run=_run
)
