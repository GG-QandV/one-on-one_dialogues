"""app/translation/supersede.py — замещение черновика проверенным. Задача D8.

Спека: раздел 3 «Правило замещения: черновик всегда замещается проверенной
версией в той же карточке. Черновик никогда не остаётся конечным результатом
в БД и экспорте».

Задача сопоставления
--------------------
Быстрый трек порождает fast-сегменты по utterance_id сегментатора; точный
трек порождает accurate-сегмент, когда реплика закрыта. Прямого общего ключа
у них нет: fast создаётся до того, как accurate существует. Сопоставление
идёт по (stream_id, перекрытие временных интервалов).

Порог перекрытия 0.5 по меньшему интервалу: точные границы accurate чуть
отличаются от границ частичных результатов (обрезка хвоста, предзапись),
поэтому строгое равенство интервалов не работает, а произвольное пересечение
в 10 мс сматчит соседние реплики.

Гарантии
--------
  * Триггер trg_segments_supersede_direction (миграция 001) не даст записать
    ссылку в обратную сторону — здесь второй рубеж, там последний.
  * Операция идемпотентна: повторный вызов для того же accurate-сегмента
    не меняет состояния.
  * Fast-сегменты без пары не остаются навсегда: expire_orphans() закрывает
    черновики старше TTL — реплика могла быть отброшена как слишком короткая.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SupersedeConfig:
    #: Минимальная доля перекрытия от меньшего интервала.
    min_overlap: float = 0.5
    #: Черновик без пары старше этого срока помечается устаревшим.
    orphan_ttl_ms: int = 30_000


@dataclass(frozen=True, slots=True)
class SupersedeResult:
    accurate_id: str
    superseded_fast_ids: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.superseded_fast_ids)


def _overlap_ratio(a0: int, a1: int, b0: int, b1: int) -> float:
    inter = min(a1, b1) - max(a0, b0)
    if inter <= 0:
        return 0.0
    shorter = min(a1 - a0, b1 - b0)
    return inter / max(1, shorter)


class SupersedeService:
    def __init__(self, db: Database, config: SupersedeConfig | None = None) -> None:
        self._db = db
        self._cfg = config or SupersedeConfig()

    async def link(self, accurate_segment_id: str) -> SupersedeResult:
        """Связать accurate-сегмент со всеми его черновиками.

        Вызывается после записи raw_text точного сегмента. Вся операция —
        одна транзакция писателя: выбор кандидатов и запись ссылок не
        разнесены во времени, гонка с параллельной вставкой fast исключена.
        """
        cfg = self._cfg

        def _tx(conn: sqlite3.Connection) -> SupersedeResult:
            acc = conn.execute(
                """
                SELECT id, stream_id, t_start_ms, t_end_ms, track
                  FROM segments WHERE id = ?
                """,
                (accurate_segment_id,),
            ).fetchone()
            if acc is None or acc["track"] != "accurate":
                return SupersedeResult(accurate_segment_id, ())

            candidates = conn.execute(
                """
                SELECT id, t_start_ms, t_end_ms
                  FROM segments
                 WHERE stream_id = ?
                   AND track = 'fast'
                   AND superseded_by_segment_id IS NULL
                   AND t_end_ms > ?
                   AND t_start_ms < ?
                """,
                (acc["stream_id"], acc["t_start_ms"], acc["t_end_ms"]),
            ).fetchall()

            matched: list[str] = [
                row["id"]
                for row in candidates
                if _overlap_ratio(
                    acc["t_start_ms"], acc["t_end_ms"],
                    row["t_start_ms"], row["t_end_ms"],
                ) >= cfg.min_overlap
            ]
            for fast_id in matched:
                conn.execute(
                    """
                    UPDATE segments SET superseded_by_segment_id = ?
                     WHERE id = ? AND superseded_by_segment_id IS NULL
                    """,
                    (accurate_segment_id, fast_id),
                )
            return SupersedeResult(accurate_segment_id, tuple(matched))

        result = await self._db.write(_tx)
        if result.count:
            log.debug(
                "сегмент %s заместил черновиков: %d",
                accurate_segment_id, result.count,
            )
        return result

    async def expire_orphans(self, now_ms: int, session_id: str) -> int:
        """Пометить осиротевшие черновики: пары не будет.

        Ссылки на несуществующий accurate не создаются; вместо этого статус
        перевода переводится в skipped — UI по нему гасит карточку черновика.
        """
        cutoff = now_ms - self._cfg.orphan_ttl_ms

        def _tx(conn: sqlite3.Connection) -> int:
            return conn.execute(
                """
                UPDATE segments
                   SET translation_status = 'skipped'
                 WHERE session_id = ?
                   AND track = 'fast'
                   AND superseded_by_segment_id IS NULL
                   AND translation_status <> 'skipped'
                   AND t_end_ms < ?
                """,
                (session_id, cutoff),
            ).rowcount

        expired = await self._db.write(_tx)
        if expired:
            log.info("устаревших черновиков без пары: %d", expired)
        return expired

    async def export_view(self, session_id: str) -> list[sqlite3.Row]:
        """Выборка для экспорта: только точный трек (критерий приёмки 13)."""
        return await self._db.fetch_all(
            """
            SELECT * FROM segments
             WHERE session_id = ? AND track = 'accurate'
             ORDER BY t_start_ms
            """,
            (session_id,),
        )

    async def stats(self, session_id: str) -> dict[str, Any]:
        rows = await self._db.fetch_all(
            """
            SELECT track,
                   COUNT(*) AS n,
                   SUM(CASE WHEN superseded_by_segment_id IS NOT NULL
                            THEN 1 ELSE 0 END) AS superseded
              FROM segments WHERE session_id = ? GROUP BY track
            """,
            (session_id,),
        )
        return {r["track"]: {"total": r["n"], "superseded": r["superseded"]} for r in rows}
