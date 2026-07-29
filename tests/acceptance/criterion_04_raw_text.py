"""Критерий 4 (§21): raw_text/таймкоды/stt_confidence в SQLite, только от
whisper, и raw_text неизменяем после первой непустой записи.

Проверка поля без проверки неизменяемости бессмысленна (H2, пункт 5) —
поэтому здесь два шага: запись, затем прямой UPDATE, который обязан
упасть.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.errors import ImmutableFieldError
from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult


async def _run(env: CheckEnv) -> CheckResult:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id, stream_id, seg_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex

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
    await env.db.execute(
        "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
        "privacy_profile, track, stt_model, detected_language, raw_text, "
        "stt_confidence, translation_status, created_at) "
        "VALUES (?, ?, ?, 0, 1200, 'open', 'accurate', 'base', 'ru', 'привет', "
        "0.87, 'pending', ?)",
        (seg_id, session_id, stream_id, now),
    )

    row = await env.db.fetch_one(
        "SELECT t_start_ms, t_end_ms, stt_confidence, raw_text FROM segments WHERE id = ?",
        (seg_id,),
    )
    if row is None:
        return CheckResult(
            4,
            "raw_text/таймкоды/confidence",
            CheckKind.AUTO,
            False,
            "сегмент не найден после записи",
        )
    if row["raw_text"] != "привет" or row["t_start_ms"] != 0 or row["t_end_ms"] != 1200:
        return CheckResult(
            4,
            "raw_text/таймкоды/confidence",
            CheckKind.AUTO,
            False,
            f"поля не совпали: {dict(row)}",
        )

    try:
        await env.db.execute("UPDATE segments SET raw_text = 'подделка' WHERE id = ?", (seg_id,))
    except ImmutableFieldError:
        pass
    else:
        return CheckResult(
            4,
            "raw_text/таймкоды/confidence",
            CheckKind.AUTO,
            False,
            "UPDATE raw_text прошёл без ошибки — инвариант неизменяемости не работает",
        )

    row2 = await env.db.fetch_one("SELECT raw_text FROM segments WHERE id = ?", (seg_id,))
    if row2["raw_text"] != "привет":
        return CheckResult(
            4,
            "raw_text/таймкоды/confidence",
            CheckKind.AUTO,
            False,
            "raw_text изменился несмотря на ошибку триггера",
        )

    return CheckResult(
        4,
        "raw_text/таймкоды/confidence",
        CheckKind.AUTO,
        True,
        "запись прошла, UPDATE raw_text отклонён триггером (ImmutableFieldError)",
    )


CHECK = CheckDef(
    number=4,
    title="raw_text/таймкоды/stt_confidence, неизменяемость",
    kind=CheckKind.AUTO,
    run=_run,
)
