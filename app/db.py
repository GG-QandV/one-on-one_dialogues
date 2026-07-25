"""app/db.py — слой хранилища. Задача B4 роадмапа.

Спека: раздел 8 «Схема данных», инвариант 8 «все записи через единый writer».

Модель конкурентности
---------------------
SQLite в режиме WAL допускает много одновременных читателей и ровно одного
писателя. Из этого следует архитектура:

  * один выделенный поток-писатель с собственным соединением;
  * пул потоков-читателей, у каждого своё соединение (read-only);
  * asyncio-код никогда не касается sqlite3 напрямую — только через futures.

Почему поток, а не asyncio-таск: модуль sqlite3 блокирующий. Попытка вызвать
его из event loop останавливает весь сервис на время записи. Попытка обойтись
одним соединением на всех — источник «database is locked» под нагрузкой,
которую даёт очередь задач.

Почему очередь, а не мьютекс: очередь даёт наблюдаемость (глубина очереди
уходит в диагностику), обратное давление (WriterQueueFull вместо неявного
роста памяти) и корректное завершение (дренаж очереди при shutdown).

Транзакции
----------
Писатель работает с isolation_level=None и явными BEGIN IMMEDIATE / COMMIT.
Неявные транзакции sqlite3 непредсказуемы: BEGIN откладывается до первой DML,
и между чтением и записью внутри одной «транзакции» может вклиниться другой
процесс. BEGIN IMMEDIATE берёт writer-lock сразу.

Инварианты
----------
Неизменяемость raw_text, privacy_profile и track защищена триггерами
(migrations/001_initial.sql). Здесь ошибки триггеров транслируются в типы из
app/errors.py, чтобы вызывающий код различал «нарушение инварианта» и
«временная ошибка среды».
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from app.errors import (
    DatabaseClosed,
    ImmutableFieldError,
    InvariantViolation,
    MigrationError,
    StorageError,
    WriterQueueFull,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Сообщения RAISE(ABORT, ...) из триггеров -> типы исключений.
_TRIGGER_MAP: dict[str, tuple[str, str]] = {
    "IMMUTABLE_RAW_TEXT": ("segments", "raw_text"),
    "IMMUTABLE_PRIVACY_PROFILE": ("segments", "privacy_profile"),
    "IMMUTABLE_TRACK": ("segments", "track"),
}

_ROW_ID_RE = re.compile(r"row_id=(\S+)")


@dataclass(frozen=True)
class DbConfig:
    """Параметры хранилища. Все значения приходят из config.toml."""

    path: Path
    busy_timeout_ms: int = 5_000
    writer_queue_max: int = 2_000
    reader_threads: int = 4
    #: Порог, после которого глубина очереди попадает в диагностику как warning.
    writer_queue_warn: int = 200
    #: Синхронный режим. NORMAL безопасен при WAL и заметно быстрее FULL.
    synchronous: str = "NORMAL"
    #: Автоматический checkpoint WAL, в страницах.
    wal_autocheckpoint: int = 1_000


@dataclass
class WriterStats:
    """Наблюдаемость писателя. Читается диагностическим экраном (E5)."""

    submitted: int = 0
    completed: int = 0
    failed: int = 0
    max_queue_depth: int = 0
    total_write_ms: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            avg = (
                self.total_write_ms / self.completed if self.completed else 0.0
            )
            return {
                "submitted": self.submitted,
                "completed": self.completed,
                "failed": self.failed,
                "max_queue_depth": self.max_queue_depth,
                "avg_write_ms": round(avg, 2),
            }


class _Shutdown:
    """Сентинел завершения работы писателя."""


_SHUTDOWN = _Shutdown()


def _translate_sqlite_error(exc: sqlite3.Error) -> Exception:
    """Превращает ошибку SQLite в доменное исключение.

    Триггеры бросают RAISE(ABORT, 'CODE'); sqlite3 отдаёт это как
    IntegrityError с текстом кода. Различать нарушение инварианта и обычную
    ошибку внешнего ключа важно: первое повторять нельзя никогда,
    второе — признак гонки, и повтор иногда осмыслен.
    """
    text = str(exc)
    for marker, (table, fieldname) in _TRIGGER_MAP.items():
        if marker in text:
            m = _ROW_ID_RE.search(text)
            return ImmutableFieldError(table, fieldname, m.group(1) if m else "?")

    for marker in (
        "FAST_TRACK_CANNOT_WRITE_RAW_TEXT",
        "INVALID_SUPERSEDE_DIRECTION",
        "DRAFT_SESSION_MISMATCH",
    ):
        if marker in text:
            return InvariantViolation(marker)

    if isinstance(exc, sqlite3.IntegrityError):
        return InvariantViolation(f"нарушение целостности: {text}")

    return StorageError(text)


class Database:
    """Единая точка доступа к SQLite.

    Использование::

        db = Database(DbConfig(path=Path("data/speech.db")))
        await db.start()
        await db.migrate(Path("migrations"))

        # запись — всегда через writer
        await db.write(lambda c: c.execute("INSERT INTO ...", params))

        # чтение — через пул читателей
        rows = await db.fetch_all("SELECT * FROM segments WHERE session_id = ?",
                                  (session_id,))

        await db.close()

    Единица записи — callable, получающий соединение. Это позволяет собрать
    несколько операций в одну атомарную транзакцию без удержания блокировки
    между awaits: весь callable исполняется внутри одного BEGIN IMMEDIATE.
    """

    def __init__(self, config: DbConfig) -> None:
        self._cfg = config
        self._write_q: queue.Queue[Any] = queue.Queue(maxsize=config.writer_queue_max)
        self._writer_thread: threading.Thread | None = None
        self._readers: concurrent.futures.ThreadPoolExecutor | None = None
        self._reader_local = threading.local()
        self._closed = threading.Event()
        self._started = False
        self.stats = WriterStats()

    # ------------------------------------------------------- жизненный цикл

    async def start(self) -> None:
        if self._started:
            return
        self._cfg.path.parent.mkdir(parents=True, exist_ok=True)

        # Инициализирующее соединение: применяет PRAGMA уровня файла (WAL
        # сохраняется в заголовке БД, поэтому достаточно установить один раз).
        init_conn = self._open(readonly=False)
        try:
            init_conn.execute("PRAGMA journal_mode = WAL")
            init_conn.execute(
                f"PRAGMA wal_autocheckpoint = {self._cfg.wal_autocheckpoint}"
            )
        finally:
            init_conn.close()

        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="db-writer", daemon=True
        )
        self._writer_thread.start()
        self._readers = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._cfg.reader_threads, thread_name_prefix="db-reader"
        )
        self._started = True
        log.info("хранилище открыто: %s", self._cfg.path)

    async def close(self, drain_timeout_s: float = 10.0) -> None:
        """Корректное завершение: дренаж очереди, checkpoint WAL, закрытие.

        Вызывается из graceful shutdown (задача F3). Незавершённые записи
        не теряются: сентинел кладётся в конец очереди, писатель обрабатывает
        всё, что было до него.
        """
        if not self._started:
            return
        self._closed.set()
        try:
            self._write_q.put(_SHUTDOWN, timeout=drain_timeout_s)
        except queue.Full:
            log.error("очередь писателя переполнена при закрытии, форсирую")
        if self._writer_thread is not None:
            await asyncio.get_running_loop().run_in_executor(
                None, self._writer_thread.join, drain_timeout_s
            )
        if self._readers is not None:
            self._readers.shutdown(wait=True, cancel_futures=False)
        self._started = False
        log.info("хранилище закрыто, статистика писателя: %s", self.stats.snapshot())

    # ------------------------------------------------------------ миграции

    async def migrate(self, migrations_dir: Path) -> int:
        """Применяет .sql-файлы по возрастанию имени. Идемпотентно.

        Версия хранится в PRAGMA user_version. Файл должен называться
        NNN_description.sql, где NNN — целевая версия.
        """
        files = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
        if not files:
            raise MigrationError(f"миграции не найдены в {migrations_dir}")

        def _apply(conn: sqlite3.Connection) -> int:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            applied = 0
            for path in files:
                try:
                    target = int(path.name[:3])
                except ValueError as exc:
                    raise MigrationError(f"плохое имя миграции: {path.name}") from exc
                if target <= current:
                    continue
                log.info("применяю миграцию %s", path.name)
                conn.executescript(path.read_text(encoding="utf-8"))
                # executescript закрывает открытую транзакцию; восстанавливаем
                # её, чтобы дальнейшие миграции шли в общем BEGIN IMMEDIATE.
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                conn.execute(f"PRAGMA user_version = {target}")
                current = target
                applied += 1
            return applied

        applied = await self.write(_apply)
        log.info("миграций применено: %d, версия схемы: %d", applied, await self.version())
        return applied

    async def version(self) -> int:
        row = await self.fetch_one("PRAGMA user_version")
        return int(row[0]) if row else 0

    # -------------------------------------------------------------- запись

    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Выполняет fn в потоке-писателе внутри одной транзакции.

        fn получает сырое соединение. Возвращённое значение прокидывается
        вызывающему. Исключение внутри fn откатывает транзакцию целиком.

        fn обязан быть синхронным и коротким. Никакого сетевого ввода-вывода
        внутри: он удерживает writer-lock и останавливает всю запись сервиса.
        """
        if self._closed.is_set():
            raise DatabaseClosed("хранилище закрывается, запись отклонена")
        if not self._started:
            raise DatabaseClosed("хранилище не инициализировано")

        future: concurrent.futures.Future[T] = concurrent.futures.Future()
        try:
            self._write_q.put_nowait((fn, future))
        except queue.Full as exc:
            raise WriterQueueFull(
                f"очередь писателя заполнена ({self._cfg.writer_queue_max})"
            ) from exc

        depth = self._write_q.qsize()
        with self.stats._lock:
            self.stats.submitted += 1
            self.stats.max_queue_depth = max(self.stats.max_queue_depth, depth)
        if depth > self._cfg.writer_queue_warn:
            log.warning("глубина очереди писателя: %d", depth)

        return await asyncio.wrap_future(future)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Одиночный DML-запрос. Возвращает число затронутых строк."""

        def _run(conn: sqlite3.Connection) -> int:
            return conn.execute(sql, params).rowcount

        return await self.write(_run)

    async def execute_many(
        self, sql: str, seq_params: Iterable[Sequence[Any]]
    ) -> int:
        rows = list(seq_params)

        def _run(conn: sqlite3.Connection) -> int:
            return conn.executemany(sql, rows).rowcount

        return await self.write(_run)

    # -------------------------------------------------------------- чтение

    async def fetch_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> sqlite3.Row | None:
        return await self._read(lambda c: c.execute(sql, params).fetchone())

    async def fetch_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[sqlite3.Row]:
        return await self._read(lambda c: c.execute(sql, params).fetchall())

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Произвольное чтение через пул читателей (несколько запросов подряд)."""
        return await self._read(fn)

    async def _read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        if not self._started:
            raise DatabaseClosed("хранилище не инициализировано")
        assert self._readers is not None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._readers, self._read_sync, fn)

    def _read_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        conn = getattr(self._reader_local, "conn", None)
        if conn is None:
            conn = self._open(readonly=True)
            self._reader_local.conn = conn
        try:
            return fn(conn)
        except sqlite3.Error as exc:
            raise _translate_sqlite_error(exc) from exc

    # ------------------------------------------------------------ внутреннее

    def _open(self, *, readonly: bool) -> sqlite3.Connection:
        if readonly and self._cfg.path.exists():
            uri = f"file:{self._cfg.path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=self._cfg.busy_timeout_ms / 1000)
        else:
            conn = sqlite3.connect(
                self._cfg.path, timeout=self._cfg.busy_timeout_ms / 1000
            )
        conn.row_factory = sqlite3.Row
        # isolation_level=None отключает неявные транзакции модуля sqlite3.
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self._cfg.busy_timeout_ms}")
        conn.execute(f"PRAGMA synchronous = {self._cfg.synchronous}")
        return conn

    def _writer_loop(self) -> None:
        conn = self._open(readonly=False)
        try:
            while True:
                item = self._write_q.get()
                if item is _SHUTDOWN:
                    break
                fn, future = item
                if not future.set_running_or_notify_cancel():
                    continue
                started = time.perf_counter()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = fn(conn)
                    conn.execute("COMMIT")
                except BaseException as exc:  # noqa: BLE001 — писатель не падает
                    self._rollback_quietly(conn)
                    with self.stats._lock:
                        self.stats.failed += 1
                    if isinstance(exc, sqlite3.Error):
                        future.set_exception(_translate_sqlite_error(exc))
                    else:
                        future.set_exception(exc)
                else:
                    elapsed = (time.perf_counter() - started) * 1000
                    with self.stats._lock:
                        self.stats.completed += 1
                        self.stats.total_write_ms += elapsed
                    future.set_result(result)
        finally:
            self._checkpoint_quietly(conn)
            conn.close()

    @staticmethod
    def _rollback_quietly(conn: sqlite3.Connection) -> None:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            log.exception("откат транзакции не удался")

    @staticmethod
    def _checkpoint_quietly(conn: sqlite3.Connection) -> None:
        """TRUNCATE-checkpoint при завершении: WAL не растёт между запусками."""
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            log.warning("checkpoint WAL при закрытии не выполнен", exc_info=True)


# ------------------------------------------------------------- утилиты

@contextmanager
def savepoint(conn: sqlite3.Connection, name: str = "sp"):
    """Вложенная точка сохранения внутри callable писателя.

    Нужна там, где часть составной операции допустимо откатить, не теряя
    остальное — например, при массовой вставке сегментов, где один сегмент
    нарушает инвариант, а прочие корректны.
    """
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")
