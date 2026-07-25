"""app/queue.py — очередь задач. Задача B5 роадмапа.

Спека: раздел 10 «Ретраи», раздел 17 «Надёжность», инвариант 7
(недоступность облака не останавливает локальный STT и запись).

Модель
------
Очередь живёт в SQLite, не в памяти. Причина прямая: критерий приёмки 16
требует, чтобы незавершённые задачи пережили перезапуск процесса. Очередь
в памяти этого не даёт.

Аренда вместо флага «running»
-----------------------------
Наивная схема ставит status='running' и полагается на воркер, который вернёт
задачу в очередь при сбое. Если процесс убит SIGKILL, воркер ничего не вернёт,
и задача останется в running навсегда. Поэтому у каждой взятой задачи есть
аренда с истечением: воркер обязан продлевать её, пока работает. Истёкшая
аренда означает мёртвого воркера — задача возвращается в очередь при
следующем сканировании или при старте процесса.

Идемпотентность
---------------
Автоматический повтор разрешён только для задач с idempotent=1. Перевод
текста идемпотентен: повторный запрос даёт эквивалентный результат.
Экспорт с побочным эффектом или доставка — нет. Неидемпотентная упавшая
задача переходит в failed и ждёт ручного повтора из истории сессий (E8).

Fencing профиля
---------------
Задача хранит профиль и поколение (app/privacy.py) на момент постановки.
Перед выполнением воркер сверяет их с текущими: задача, поставленная в
открытом профиле и требующая облака, не выполняется после перехода в
конфиденциальный. Это второй рубеж после проверки в самом провайдере.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from app.db import Database
from app.errors import JobNotFound, LeaseLost, NonRetryableJob
from app.privacy import Capability, Fence, PrivacyController, PrivacyProfile

log = logging.getLogger(__name__)


class JobType(str, Enum):
    STT = "stt"
    TRANSLATE = "translate"
    DRAFT = "draft"
    EXPORT = "export"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Какая возможность нужна задаче. Сверяется с профилем перед выполнением.
JOB_CAPABILITY: dict[JobType, Capability] = {
    JobType.STT: Capability.LOCAL_STT,
    JobType.TRANSLATE: Capability.TEXT_TO_CLOUD,
    JobType.DRAFT: Capability.DRAFT_GENERATION,
    JobType.EXPORT: Capability.LOCAL_EXPORT,
}


@dataclass(frozen=True, slots=True)
class QueueConfig:
    lease_ttl_s: int = 60
    #: Продлевать аренду, когда истекло больше этой доли TTL.
    lease_renew_at: float = 0.5
    poll_interval_s: float = 0.25
    #: Базовая задержка экспоненциального отката.
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60.0
    #: Джиттер: доля от расчётной задержки, добавляемая случайно.
    backoff_jitter: float = 0.25
    max_attempts_default: int = 3
    #: Сколько задач каждого типа выполнять одновременно.
    concurrency: dict[str, int] = None  # type: ignore[assignment]

    def workers_for(self, job_type: JobType) -> int:
        defaults = {
            JobType.STT: 1,  # один экземпляр whisper — спека, раздел 7
            JobType.TRANSLATE: 3,
            JobType.DRAFT: 1,
            JobType.EXPORT: 1,
        }
        if self.concurrency:
            return int(self.concurrency.get(job_type.value, defaults[job_type]))
        return defaults[job_type]


@dataclass(slots=True)
class Job:
    id: str
    type: JobType
    segment_id: str | None
    payload: dict[str, Any]
    status: JobStatus
    idempotent: bool
    attempts: int
    max_attempts: int
    privacy_profile: PrivacyProfile
    privacy_gen: int
    lease_owner: str | None
    error_code: str | None

    @property
    def fence(self) -> Fence:
        return Fence(self.privacy_profile, self.privacy_gen)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            type=JobType(row["type"]),
            segment_id=row["segment_id"],
            payload=json.loads(row["payload_json"] or "{}"),
            status=JobStatus(row["status"]),
            idempotent=bool(row["idempotent"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            privacy_profile=PrivacyProfile(row["privacy_profile"]),
            privacy_gen=int(row["privacy_gen"]),
            lease_owner=row["lease_owner"],
            error_code=row["error_code"],
        )


#: Обработчик задачи. Получает Job, возвращает произвольный результат.
#: Исключение означает провал попытки; тип исключения решает, будет ли ретрай.
Handler = Callable[[Job], Awaitable[Any]]


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


class JobQueue:
    """Персистентная очередь задач поверх SQLite."""

    def __init__(
        self,
        db: Database,
        privacy: PrivacyController,
        config: QueueConfig | None = None,
    ) -> None:
        self._db = db
        self._privacy = privacy
        self._cfg = config or QueueConfig()
        self._owner = f"{uuid.uuid4().hex[:8]}"
        self._handlers: dict[JobType, Handler] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._paused: set[JobType] = set()

    # ------------------------------------------------------------ постановка

    async def enqueue(
        self,
        job_type: JobType,
        *,
        segment_id: str | None = None,
        payload: dict[str, Any] | None = None,
        idempotent: bool = True,
        idempotency_key: str | None = None,
        max_attempts: int | None = None,
        delay_s: float = 0.0,
    ) -> str:
        """Поставить задачу. Возвращает id.

        При переданном idempotency_key повторная постановка той же задачи
        не создаёт дубликат: сработает уникальный индекс, и вернётся id
        существующей задачи. Это защита от двойной постановки при повторной
        обработке одного сегмента после реконнекта.
        """
        job_id = uuid.uuid4().hex
        now = _now()
        next_at = now + timedelta(seconds=delay_s)
        fence = self._privacy.fence()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        attempts_limit = max_attempts or self._cfg.max_attempts_default

        def _insert(conn: sqlite3.Connection) -> str:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT id FROM jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    return existing["id"]
            conn.execute(
                """
                INSERT INTO jobs (id, type, segment_id, payload_json, status,
                                  idempotent, idempotency_key, attempts,
                                  max_attempts, next_attempt_at,
                                  privacy_profile, privacy_gen,
                                  created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type.value,
                    segment_id,
                    payload_json,
                    int(idempotent),
                    idempotency_key,
                    attempts_limit,
                    _iso(next_at),
                    fence.profile.value,
                    fence.generation,
                    _iso(now),
                    _iso(now),
                ),
            )
            return job_id

        return await self._db.write(_insert)

    # ---------------------------------------------------------------- взятие

    async def _claim(self, job_type: JobType) -> Job | None:
        """Атомарно взять одну готовую задачу и поставить аренду.

        Выборка и обновление выполняются внутри одного BEGIN IMMEDIATE, так
        что двое воркеров не возьмут одну задачу. Именно поэтому claim идёт
        через writer, а не через читателя: чтение и запись должны быть в одной
        транзакции.
        """
        now = _now()
        lease_until = now + timedelta(seconds=self._cfg.lease_ttl_s)

        def _pick(conn: sqlite3.Connection) -> sqlite3.Row | None:
            row = conn.execute(
                """
                SELECT * FROM jobs
                 WHERE type = ?
                   AND status = 'queued'
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                 ORDER BY next_attempt_at ASC, created_at ASC
                 LIMIT 1
                """,
                (job_type.value, _iso(now)),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE jobs
                   SET status = 'running',
                       lease_owner = ?,
                       lease_expires_at = ?,
                       attempts = attempts + 1,
                       updated_at = ?
                 WHERE id = ? AND status = 'queued'
                """,
                (self._owner, _iso(lease_until), _iso(now), row["id"]),
            )
            return conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()

        row = await self._db.write(_pick)
        return Job.from_row(row) if row is not None else None

    async def _renew_lease(self, job_id: str) -> None:
        lease_until = _now() + timedelta(seconds=self._cfg.lease_ttl_s)

        def _renew(conn: sqlite3.Connection) -> int:
            return conn.execute(
                """
                UPDATE jobs
                   SET lease_expires_at = ?, updated_at = ?
                 WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (_iso(lease_until), _iso(_now()), job_id, self._owner),
            ).rowcount

        if await self._db.write(_renew) == 0:
            raise LeaseLost(f"аренда задачи {job_id} потеряна")

    # -------------------------------------------------------------- финализация

    async def _complete(self, job_id: str) -> None:
        def _done(conn: sqlite3.Connection) -> int:
            return conn.execute(
                """
                UPDATE jobs
                   SET status = 'done', lease_owner = NULL,
                       lease_expires_at = NULL, error_code = NULL,
                       error_detail = NULL, updated_at = ?
                 WHERE id = ? AND lease_owner = ?
                """,
                (_iso(_now()), job_id, self._owner),
            ).rowcount

        if await self._db.write(_done) == 0:
            raise LeaseLost(f"результат задачи {job_id} не принят: аренда потеряна")

    async def _fail(self, job: Job, exc: BaseException) -> None:
        """Решить судьбу упавшей задачи: повтор или окончательный провал."""
        code = getattr(exc, "code", exc.__class__.__name__)
        retryable = bool(getattr(exc, "retryable", False))
        detail = str(exc)[:500]

        exhausted = job.attempts >= job.max_attempts
        may_retry = job.idempotent and retryable and not exhausted

        if may_retry:
            delay = self._backoff(job.attempts)
            next_at = _now() + timedelta(seconds=delay)
            log.warning(
                "задача %s (%s) упала [%s], попытка %d/%d, повтор через %.1f с",
                job.id, job.type.value, code, job.attempts, job.max_attempts, delay,
            )
            new_status = JobStatus.QUEUED
        else:
            next_at = None
            reason = (
                "исчерпаны попытки" if exhausted
                else "неидемпотентная" if not job.idempotent
                else "неповторяемая ошибка"
            )
            log.error(
                "задача %s (%s) провалена [%s]: %s",
                job.id, job.type.value, code, reason,
            )
            new_status = JobStatus.FAILED

        def _update(conn: sqlite3.Connection) -> int:
            return conn.execute(
                """
                UPDATE jobs
                   SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                       next_attempt_at = ?, error_code = ?, error_detail = ?,
                       updated_at = ?
                 WHERE id = ? AND lease_owner = ?
                """,
                (
                    new_status.value,
                    _iso(next_at) if next_at else None,
                    code,
                    detail,
                    _iso(_now()),
                    job.id,
                    self._owner,
                ),
            ).rowcount

        await self._db.write(_update)

    def _backoff(self, attempt: int) -> float:
        raw = self._cfg.backoff_base_s * (2 ** max(0, attempt - 1))
        capped = min(raw, self._cfg.backoff_max_s)
        jitter = capped * self._cfg.backoff_jitter * random.random()
        return capped + jitter

    # ------------------------------------------------------------ восстановление

    async def recover(self) -> dict[str, int]:
        """Восстановление при старте. Задача F4 роадмапа.

        Возвращает в очередь задачи с истёкшей арендой. Неидемпотентные
        переводит в failed: повторять их автоматически нельзя, потому что
        неизвестно, успел ли предыдущий запуск произвести побочный эффект.
        """
        now = _iso(_now())

        def _recover(conn: sqlite3.Connection) -> dict[str, int]:
            requeued = conn.execute(
                """
                UPDATE jobs
                   SET status = 'queued', lease_owner = NULL,
                       lease_expires_at = NULL, next_attempt_at = ?,
                       error_code = 'LEASE_EXPIRED', updated_at = ?
                 WHERE status = 'running'
                   AND idempotent = 1
                   AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (now, now, now),
            ).rowcount
            failed = conn.execute(
                """
                UPDATE jobs
                   SET status = 'failed', lease_owner = NULL,
                       lease_expires_at = NULL,
                       error_code = 'NON_IDEMPOTENT_INTERRUPTED', updated_at = ?
                 WHERE status = 'running'
                   AND idempotent = 0
                   AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (now, now),
            ).rowcount
            return {"requeued": requeued, "failed": failed}

        result = await self._db.write(_recover)
        if result["requeued"] or result["failed"]:
            log.warning(
                "восстановление очереди: возвращено %d, провалено %d",
                result["requeued"], result["failed"],
            )
        return result

    async def retry_manually(self, job_id: str) -> None:
        """Ручной повтор из истории сессий (E8). Разрешён и для failed."""

        def _retry(conn: sqlite3.Connection) -> int:
            return conn.execute(
                """
                UPDATE jobs
                   SET status = 'queued', attempts = 0, next_attempt_at = ?,
                       error_code = NULL, error_detail = NULL, updated_at = ?
                 WHERE id = ? AND status IN ('failed', 'cancelled')
                """,
                (_iso(_now()), _iso(_now()), job_id),
            ).rowcount

        if await self._db.write(_retry) == 0:
            raise JobNotFound(f"задача {job_id} не найдена или не в failed")

    async def cancel_by_fence(self, capability: Capability) -> int:
        """Отменить очередные задачи, ставшие недопустимыми после переключения.

        Вызывается слушателем PrivacyController. Уже выполняющиеся задачи
        отсекаются проверкой fence в воркере, очередные — здесь.
        """
        types = [t.value for t, cap in JOB_CAPABILITY.items() if cap == capability]
        if not types:
            return 0
        placeholders = ",".join("?" * len(types))

        def _cancel(conn: sqlite3.Connection) -> int:
            return conn.execute(
                f"""
                UPDATE jobs
                   SET status = 'cancelled', error_code = 'PRIVACY_SWITCH',
                       updated_at = ?
                 WHERE status = 'queued' AND type IN ({placeholders})
                """,
                (_iso(_now()), *types),
            ).rowcount

        cancelled = await self._db.write(_cancel)
        if cancelled:
            log.warning("отменено задач при переключении профиля: %d", cancelled)
        return cancelled

    # -------------------------------------------------------------- воркеры

    def register(self, job_type: JobType, handler: Handler) -> None:
        self._handlers[job_type] = handler

    def pause(self, job_type: JobType) -> None:
        """Приостановить тип задач. Используется каскадом деградации (F2)."""
        self._paused.add(job_type)
        log.warning("приём задач типа %s приостановлен", job_type.value)

    def resume(self, job_type: JobType) -> None:
        self._paused.discard(job_type)

    async def start(self) -> None:
        await self.recover()
        for job_type, _ in self._handlers.items():
            for i in range(self._cfg.workers_for(job_type)):
                task = asyncio.create_task(
                    self._worker_loop(job_type),
                    name=f"jobworker:{job_type.value}:{i}",
                )
                self._tasks.append(task)
        self._tasks.append(
            asyncio.create_task(self._reaper_loop(), name="jobqueue:reaper")
        )
        log.info("очередь запущена, воркеров: %d", len(self._tasks) - 1)

    async def stop(self, timeout_s: float = 10.0) -> None:
        """Остановка: воркеры дорабатывают текущую задачу и выходят."""
        self._stop.set()
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=timeout_s)
        for task in pending:
            log.warning("воркер %s не завершился, отменяю", task.get_name())
            task.cancel()
        self._tasks.clear()

    async def _worker_loop(self, job_type: JobType) -> None:
        handler = self._handlers[job_type]
        while not self._stop.is_set():
            if job_type in self._paused:
                await self._sleep_or_stop(self._cfg.poll_interval_s)
                continue
            try:
                job = await self._claim(job_type)
            except Exception:  # noqa: BLE001 — воркер не умирает от сбоя БД
                log.exception("не удалось взять задачу типа %s", job_type.value)
                await self._sleep_or_stop(1.0)
                continue

            if job is None:
                await self._sleep_or_stop(self._cfg.poll_interval_s)
                continue

            await self._run_one(job, handler)

    async def _run_one(self, job: Job, handler: Handler) -> None:
        """Выполнить задачу с продлением аренды и обработкой исхода."""
        capability = JOB_CAPABILITY[job.type]

        # Второй рубеж fencing: профиль мог смениться, пока задача ждала.
        if not self._privacy.allows(capability):
            log.info(
                "задача %s (%s) отменена: профиль %s не допускает %s",
                job.id, job.type.value, self._privacy.profile.value, capability.value,
            )
            await self._fail(job, NonRetryableJob("PRIVACY_SWITCH"))
            return

        renew = asyncio.create_task(
            self._lease_keeper(job.id), name=f"lease:{job.id}"
        )
        try:
            await handler(job)
        except asyncio.CancelledError:
            await self._fail(job, NonRetryableJob("CANCELLED"))
            raise
        except BaseException as exc:  # noqa: BLE001
            await self._fail(job, exc)
        else:
            try:
                await self._complete(job.id)
            except LeaseLost:
                # Результат посчитан, но аренда перехвачена: чужой воркер уже
                # переработал задачу. Свой результат не навязываем.
                log.warning("результат задачи %s отброшен: аренда потеряна", job.id)
        finally:
            renew.cancel()

    async def _lease_keeper(self, job_id: str) -> None:
        interval = self._cfg.lease_ttl_s * self._cfg.lease_renew_at
        try:
            while True:
                await asyncio.sleep(interval)
                await self._renew_lease(job_id)
        except asyncio.CancelledError:
            pass
        except LeaseLost:
            log.error("аренда задачи %s потеряна во время выполнения", job_id)

    async def _reaper_loop(self) -> None:
        """Периодический возврат задач с истёкшей арендой чужих процессов."""
        while not self._stop.is_set():
            await self._sleep_or_stop(self._cfg.lease_ttl_s)
            if self._stop.is_set():
                return
            try:
                await self.recover()
            except Exception:  # noqa: BLE001
                log.exception("сканирование истёкших аренд не удалось")

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    # ------------------------------------------------------------ наблюдаемость

    async def stats(self) -> dict[str, Any]:
        """Сводка для диагностического экрана (E5)."""
        rows = await self._db.fetch_all(
            "SELECT type, status, COUNT(*) AS n FROM jobs GROUP BY type, status"
        )
        by_type: dict[str, dict[str, int]] = {}
        for row in rows:
            by_type.setdefault(row["type"], {})[row["status"]] = int(row["n"])
        oldest = await self._db.fetch_one(
            """
            SELECT MIN(next_attempt_at) AS t FROM jobs WHERE status = 'queued'
            """
        )
        return {
            "owner": self._owner,
            "by_type": by_type,
            "paused": sorted(t.value for t in self._paused),
            "oldest_queued_at": oldest["t"] if oldest else None,
        }
