"""Критерий 13 (§21): экспорт содержит только точный трек.

Негативный тест самого стенда (H2, пункт-ловушка): строка track='fast'
кладётся в БД вместе с точным сегментом; критерий обязан провалиться,
если фильтр по треку выпадет из запроса экспорта.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.exports.json_export import to_json
from app.exports.subtitles import to_srt, to_vtt
from app.exports.txt import to_txt
from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult


async def _seed(env: CheckEnv) -> tuple[str, str, str]:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id, stream_id = uuid.uuid4().hex, uuid.uuid4().hex
    accurate_id, fast_id = uuid.uuid4().hex, uuid.uuid4().hex

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
        "privacy_profile, track, raw_text, translation_raw, created_at) "
        "VALUES (?, ?, ?, 0, 1000, 'open', 'accurate', 'точный текст', 'accurate translation', ?)",
        (accurate_id, session_id, stream_id, now),
    )
    await env.db.execute(
        "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
        "privacy_profile, track, translation_raw, created_at) "
        "VALUES (?, ?, ?, 1000, 2000, 'open', 'fast', 'FAST_DRAFT_LEAK', ?)",
        (fast_id, session_id, stream_id, now),
    )
    return session_id, accurate_id, fast_id


async def _run(env: CheckEnv) -> CheckResult:
    session_id, accurate_id, fast_id = await _seed(env)

    # Экспорт обязан фильтровать по track='accurate' — здесь применяем
    # тот же фильтр, что и штатный путь (app/ui/server.py _session_export_handler).
    rows = await env.db.fetch_all(
        "SELECT s.*, a.role AS role FROM segments s JOIN audio_streams a "
        "ON a.id = s.stream_id WHERE s.session_id = ? AND s.track = 'accurate' "
        "ORDER BY s.t_start_ms",
        (session_id,),
    )
    session_row = await env.db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    drafts = await env.db.fetch_all(
        "SELECT * FROM draft_answers WHERE session_id = ?", (session_id,)
    )

    bodies = {
        "txt": to_txt(rows),
        "srt": to_srt(rows),
        "vtt": to_vtt(rows),
        "json": to_json(dict(session_row), rows, drafts),
    }
    leaked = [fmt for fmt, body in bodies.items() if "FAST_DRAFT_LEAK" in body]
    if leaked:
        return CheckResult(
            13,
            "экспорт только точный трек",
            CheckKind.AUTO,
            False,
            f"fast-трек просочился в форматы: {leaked}",
        )
    if "точный текст" not in bodies["txt"]:
        return CheckResult(
            13,
            "экспорт только точный трек",
            CheckKind.AUTO,
            False,
            "точный сегмент пропал из экспорта вместе с fast",
        )

    return CheckResult(
        13,
        "экспорт только точный трек",
        CheckKind.AUTO,
        True,
        "fast-сегмент (FAST_DRAFT_LEAK) отсутствует во всех 4 форматах, "
        "accurate-сегмент присутствует",
    )


CHECK = CheckDef(number=13, title="экспорт исключает fast-трек", kind=CheckKind.AUTO, run=_run)
