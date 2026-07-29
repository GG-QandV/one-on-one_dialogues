"""Критерий 16 (§21): остановка/перезапуск не повреждают сессию, задачи
восстанавливаются.

Ловушка спеки: нужен именно SIGKILL, а не graceful stop() — корректное
завершение проверяет другой путь. Здесь это моделируется тем, что реально
происходит после SIGKILL: задача осталась в статусе 'running' с
lease_owner мёртвого процесса и истёкшей арендой (никто не успел её
продлить или закрыть). Fresh JobQueue.recover() при "перезапуске" обязан
вернуть идемпотентную задачу в очередь и провалить неидемпотентную — так
её не повторят вслепую.

Ограничение проверки: это не настоящий os.kill(SIGKILL) по PID процесса,
а прямое воспроизведение состояния БД, которое SIGKILL оставляет. Более
строгая LIVE-версия с реальным убийством процесса — отдельная задача,
если стенд когда-нибудь придётся гонять на настоящем бинарнике.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.privacy import PrivacyController, PrivacyProfile
from app.queue import JobQueue, QueueConfig
from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


async def _run(env: CheckEnv) -> CheckResult:
    now = datetime.now(UTC)
    idempotent_id = uuid.uuid4().hex
    non_idempotent_id = uuid.uuid4().hex

    def _insert(conn):
        conn.execute(
            """INSERT INTO jobs (id, type, payload_json, status,
              idempotent, attempts, max_attempts,
              privacy_profile, privacy_gen, created_at, updated_at,
              lease_owner, lease_expires_at)
              VALUES (?, 'translate', '{}', 'running',
                      1, 1, 3, 'open', 0, ?, ?, 'killed-process-pid-9999', ?)""",
            (idempotent_id, _iso(now), _iso(now), _iso(now - timedelta(hours=1))),
        )
        conn.execute(
            """INSERT INTO jobs (id, type, payload_json, status,
              idempotent, attempts, max_attempts,
              privacy_profile, privacy_gen, created_at, updated_at,
              lease_owner, lease_expires_at)
              VALUES (?, 'draft', '{}', 'running',
                      0, 1, 3, 'open', 0, ?, ?, 'killed-process-pid-9999', ?)""",
            (non_idempotent_id, _iso(now), _iso(now), _iso(now - timedelta(hours=1))),
        )

    await env.db.write(_insert)

    # "Перезапуск": новый процесс, новый JobQueue поверх той же БД.
    privacy = PrivacyController(PrivacyProfile.OPEN)
    fresh_queue = JobQueue(env.db, privacy, QueueConfig())
    result = await fresh_queue.recover()

    idem_row = await env.db.fetch_one("SELECT status FROM jobs WHERE id = ?", (idempotent_id,))
    nonidem_row = await env.db.fetch_one(
        "SELECT status FROM jobs WHERE id = ?", (non_idempotent_id,)
    )

    title = "SIGKILL: сессия восстанавливается"
    if idem_row["status"] != "queued":
        detail = f"идемпотентная задача осталась {idem_row['status']}, ожидался queued"
        return CheckResult(16, title, CheckKind.AUTO, False, detail)
    if nonidem_row["status"] != "failed":
        detail = (
            f"неидемпотентная задача осталась {nonidem_row['status']}, "
            "ожидался failed (не повторяем вслепую)"
        )
        return CheckResult(16, title, CheckKind.AUTO, False, detail)
    if result.get("requeued") != 1 or result.get("failed") != 1:
        detail = f"recover() вернул {result}, ожидалось requeued=1, failed=1"
        return CheckResult(16, title, CheckKind.AUTO, False, detail)

    return CheckResult(
        16,
        title,
        CheckKind.AUTO,
        True,
        "после симулированного SIGKILL (running + просроченная аренда мёртвого владельца) "
        "recover() вернул идемпотентную задачу в очередь и провалил неидемпотентную",
    )


CHECK = CheckDef(
    number=16,
    title="перезапуск после SIGKILL восстанавливает задачи",
    kind=CheckKind.AUTO,
    run=_run,
)
