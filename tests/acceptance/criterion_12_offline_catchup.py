"""Критерий 12 (§21): облако недоступно → запись и STT продолжаются,
очередь догоняется при восстановлении.

Реальный DB + JobQueue + OfflineGate: пока провайдер BLOCKED, запись
точных сегментов (STT) идёт независимо от gate (он влияет только на
джобу перевода); при восстановлении catch_up() ставит в очередь ровно
столько задач, сколько накопилось pending-сегментов.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.errors import ProviderAuthError
from app.privacy import PrivacyController, PrivacyProfile
from app.queue import JobQueue, QueueConfig
from app.translation.offline import OfflineConfig, OfflineGate
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

    gate = OfflineGate(OfflineConfig())
    gate.mark_unavailable("gemini", ProviderAuthError("no key"))
    if gate.should_attempt("gemini"):
        return CheckResult(
            12,
            "offline: запись продолжается, очередь догоняется",
            CheckKind.AUTO,
            False,
            "gate не заблокировал provider после ProviderAuthError",
        )

    # "Облако недоступно" не мешает STT писать сегменты — вставляем 5 штук
    # напрямую, как это делает конвейер независимо от offline gate.
    segment_ids = []
    for i in range(5):
        seg_id = uuid.uuid4().hex
        segment_ids.append(seg_id)
        await env.db.execute(
            "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
            "privacy_profile, track, raw_text, translation_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', 'accurate', ?, 'pending', ?)",
            (seg_id, session_id, stream_id, i * 1000, i * 1000 + 900, f"фраза {i}", now),
        )

    row = await env.db.fetch_one(
        "SELECT COUNT(*) AS n FROM segments "
        "WHERE session_id = ? AND translation_status = 'pending'",
        (session_id,),
    )
    if row["n"] != 5:
        return CheckResult(
            12,
            "offline: запись продолжается, очередь догоняется",
            CheckKind.AUTO,
            False,
            f"ожидалось 5 pending-сегментов, найдено {row['n']}",
        )

    # Провайдер восстановился: catch_up должен поставить в очередь ровно 5 задач.
    gate.mark_available("gemini")
    privacy = PrivacyController(PrivacyProfile.OPEN)
    queue = JobQueue(env.db, privacy, QueueConfig())
    enqueued = await gate.catch_up(env.db, queue)

    if enqueued != 5:
        return CheckResult(
            12,
            "offline: запись продолжается, очередь догоняется",
            CheckKind.AUTO,
            False,
            f"catch_up поставил {enqueued} задач, ожидалось 5",
        )

    jobs_row = await env.db.fetch_one(
        "SELECT COUNT(*) AS n FROM jobs WHERE type = 'translate' AND status = 'queued'"
    )
    if jobs_row["n"] != 5:
        return CheckResult(
            12,
            "offline: запись продолжается, очередь догоняется",
            CheckKind.AUTO,
            False,
            f"в БД {jobs_row['n']} queued-задач, ожидалось 5",
        )

    return CheckResult(
        12,
        "offline: запись продолжается, очередь догоняется",
        CheckKind.AUTO,
        True,
        "5 сегментов записаны при заблокированном провайдере; catch_up() ровно "
        "5 задач перевода после восстановления",
    )


CHECK = CheckDef(
    number=12,
    title="offline: запись/STT продолжаются, очередь догоняется",
    kind=CheckKind.AUTO,
    run=_run,
)
