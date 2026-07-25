"""app/drafts/guardrails.py — ограничители генератора черновиков. Задача I5.

Спека: раздел 12 «Обязательные ограничители». Роадмап, раздел 17: черновик
с фактом вне библиотеки без пометки has_gaps — провал этапа; любой путь
автодоставки — провал этапа.

Модель угроз
------------
Опасность не в том, что модель ответит плохо, а в том, что пользователь
зачитает клиенту выдуманную цену. Отсюда три рубежа:

1. **Верификация чисел.** Каждое число, сумма, процент и срок в черновике
   обязаны встречаться в библиотеке. Число без источника — либо has_gaps,
   либо отказ от черновика целиком (настраивается).
2. **Изоляция полей.** Запись черновика возможна только в draft_answers.
   Санитайзер вызывается на единственном пути записи; попытка записать
   draft-контент в segments.* перехватывается на уровне схемы (поля не
   пересекаются) и на уровне кода (отдельный сервис без доступа к segments).
3. **Отсутствие автодоставки.** Модуль не имеет ни одного метода, который
   отдаёт текст наружу процесса. Доставка живёт в app/delivery и требует
   явного действия пользователя (status='copied' ставится оттуда).

Числовая верификация — почему токены, а не строки
-------------------------------------------------
«30 000 евро», «30000€» и «30к EUR» — одно число в трёх написаниях.
Сравнение нормализованных числовых значений ловит все три; сравнение
подстрок — ни одного из перекрёстных вариантов.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from app.db import Database
from app.errors import InvariantViolation

log = logging.getLogger(__name__)

#: Числа с разделителями тысяч, десятичной точкой/запятой и суффиксами к/k/м/m.
_NUM_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[ \u00a0.,]\d{3})+|\d+)(?:[.,](\d+))?\s*([кkмm])?",
    re.IGNORECASE,
)

_SUFFIX = {"к": 1_000, "k": 1_000, "м": 1_000_000, "m": 1_000_000}

#: Числа, не считающиеся «фактами»: нумерация списков, проценты до 100
#: сами по себе фактом не являются только если равны типовым (см. ниже).
_TRIVIAL = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100}


def extract_numbers(text: str) -> set[float]:
    """Нормализованные числовые значения текста."""
    values: set[float] = set()
    for m in _NUM_RE.finditer(text):
        whole = re.sub(r"[ \u00a0.,]", "", m.group(1))
        frac = m.group(2) or ""
        try:
            value = float(f"{whole}.{frac}" if frac else whole)
        except ValueError:
            continue
        mult = _SUFFIX.get((m.group(3) or "").lower(), 1)
        values.add(value * mult)
    return values


class VerdictKind(str, Enum):
    ACCEPT = "accept"                # черновик чист
    ACCEPT_WITH_GAPS = "accept_gaps" # есть непокрытые числа, помечен
    REJECT = "reject"                # нарушение, черновик не сохраняется


@dataclass(frozen=True, slots=True)
class GuardConfig:
    #: reject вместо accept_gaps при числах вне библиотеки. Для переговоров
    #: о деньгах строгий режим — рекомендуемый.
    strict_numbers: bool = True
    max_words: int = 120
    #: Обязательная фраза-мета при пробелах (из промпта DraftProvider).
    gap_marker: str = "нет данных"


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: VerdictKind
    unverified_numbers: tuple[float, ...]
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.kind is not VerdictKind.REJECT


@dataclass(frozen=True, slots=True)
class DraftCandidate:
    """То, что вернул DraftProvider (I2) до проверки."""

    session_id: str
    trigger_segment_id: str
    draft_ru: str
    target_language: str
    sources: tuple[str, ...]
    has_gaps_claimed: bool
    gap_note: str | None


class DraftGuard:
    """Верификация и единственный путь записи черновика."""

    def __init__(self, db: Database, config: GuardConfig | None = None) -> None:
        self._db = db
        self._cfg = config or GuardConfig()

    # ------------------------------------------------------------ верификация

    def verify(self, candidate: DraftCandidate, library_text: str) -> Verdict:
        reasons: list[str] = []

        words = len(candidate.draft_ru.split())
        if words > self._cfg.max_words * 1.5:
            reasons.append(f"длина {words} слов при лимите {self._cfg.max_words}")

        draft_nums = {
            v for v in extract_numbers(candidate.draft_ru) if v not in _TRIVIAL
        }
        library_nums = extract_numbers(library_text)
        unverified = tuple(sorted(draft_nums - library_nums))

        if unverified:
            gap_declared = (
                candidate.has_gaps_claimed
                or self._cfg.gap_marker in candidate.draft_ru.lower()
            )
            if self._cfg.strict_numbers:
                reasons.append(
                    f"числа вне библиотеки: {unverified} — строгий режим"
                )
                return Verdict(VerdictKind.REJECT, unverified, tuple(reasons))
            if not gap_declared:
                reasons.append("числа вне библиотеки без пометки has_gaps")
                return Verdict(VerdictKind.REJECT, unverified, tuple(reasons))
            return Verdict(VerdictKind.ACCEPT_WITH_GAPS, unverified, tuple(reasons))

        if not candidate.sources and not candidate.has_gaps_claimed:
            # Ответ «из воздуха»: источников нет и пробелы не заявлены.
            reasons.append("нет источников и нет заявленных пробелов")
            return Verdict(VerdictKind.REJECT, (), tuple(reasons))

        if reasons:
            return Verdict(VerdictKind.REJECT, (), tuple(reasons))
        kind = (
            VerdictKind.ACCEPT_WITH_GAPS
            if candidate.has_gaps_claimed
            else VerdictKind.ACCEPT
        )
        return Verdict(kind, (), ())

    # ----------------------------------------------------------------- запись

    async def store(
        self, candidate: DraftCandidate, verdict: Verdict
    ) -> str | None:
        """Записать принятый черновик. Отклонённый — только в лог.

        Возвращает id записи или None при отказе. Запись идёт исключительно
        в draft_answers; сегментов этот сервис не касается по построению —
        у него нет ни одного SQL к segments.
        """
        if not verdict.accepted:
            log.warning(
                "черновик отклонён (%s): %s",
                candidate.trigger_segment_id, "; ".join(verdict.reasons),
            )
            return None

        draft_id = uuid.uuid4().hex
        has_gaps = verdict.kind is VerdictKind.ACCEPT_WITH_GAPS
        now = datetime.now(UTC).isoformat(timespec="milliseconds")

        def _tx(conn: sqlite3.Connection) -> str:
            try:
                conn.execute(
                    """
                    INSERT INTO draft_answers
                           (id, session_id, trigger_segment_id, draft_ru,
                            target_language, sources_json, has_gaps, gap_note,
                            status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'generated', ?)
                    """,
                    (
                        draft_id,
                        candidate.session_id,
                        candidate.trigger_segment_id,
                        candidate.draft_ru,
                        candidate.target_language,
                        json.dumps(list(candidate.sources), ensure_ascii=False),
                        int(has_gaps),
                        candidate.gap_note,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "idx_drafts_trigger" in str(exc) or "UNIQUE" in str(exc):
                    # На один вопрос — один черновик: повторная генерация
                    # (ретрай задачи) не плодит дубликаты.
                    row = conn.execute(
                        "SELECT id FROM draft_answers WHERE trigger_segment_id = ?",
                        (candidate.trigger_segment_id,),
                    ).fetchone()
                    return row["id"] if row else draft_id
                raise
            return draft_id

        stored = await self._db.write(_tx)
        log.info(
            "черновик %s сохранён (%s, gaps=%s)",
            stored, verdict.kind.value, has_gaps,
        )
        return stored

    async def attach_translation(self, draft_id: str, translated: str) -> None:
        """Перевод черновика (I4) дописывается отдельно: генерация и перевод —
        разные облачные вызовы, второй может упасть независимо."""
        await self._db.execute(
            "UPDATE draft_answers SET draft_translated = ? WHERE id = ?",
            (translated, draft_id),
        )

    # ------------------------------------------------------- статусы действий

    async def mark(self, draft_id: str, status: str) -> None:
        """'ignored' — пользователь пролистал; 'copied' — скопировал.

        'copied' ставит ТОЛЬКО app/delivery по факту явного действия.
        Прямых вызовов из генератора быть не должно — проверяется грепом
        в CI (check_no_stubs.sh расширяется правилом на 'copied').
        """
        if status not in ("ignored", "copied"):
            raise InvariantViolation(f"недопустимый статус черновика: {status}")
        await self._db.execute(
            "UPDATE draft_answers SET status = ? WHERE id = ?", (status, draft_id)
        )

    async def stats(self, session_id: str) -> dict[str, Any]:
        rows = await self._db.fetch_all(
            """
            SELECT status, has_gaps, COUNT(*) AS n
              FROM draft_answers WHERE session_id = ?
             GROUP BY status, has_gaps
            """,
            (session_id,),
        )
        return {
            f"{r['status']}{'+gaps' if r['has_gaps'] else ''}": r["n"] for r in rows
        }
