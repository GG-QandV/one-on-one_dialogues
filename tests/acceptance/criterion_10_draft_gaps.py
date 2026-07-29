"""Критерий 10 (§21): черновик генерируется на вопрос, содержит источники,
и получает has_gaps при нехватке фактов в библиотеке.

Три сценария на реальном DraftGuard.verify (I3): факт есть в библиотеке →
ACCEPT с источником; факта нет, пробел заявлен → ACCEPT_WITH_GAPS; факта
нет, пробел НЕ заявлен → REJECT (guard обязан поймать "ответ из воздуха").
"""

from __future__ import annotations

from app.drafts.guardrails import DraftCandidate, DraftGuard, VerdictKind
from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult

LIBRARY_TEXT = "Тариф Pro стоит 4900 рублей в месяц. Поддержка 24/7."


async def _run(env: CheckEnv) -> CheckResult:
    guard = DraftGuard(env.db)

    with_fact = DraftCandidate(
        session_id="s1",
        trigger_segment_id="seg1",
        draft_ru="Тариф Pro стоит 4900 рублей в месяц.",
        target_language="en",
        sources=("Тариф Pro стоит 4900 рублей в месяц.",),
        has_gaps_claimed=False,
        gap_note=None,
    )
    v1 = guard.verify(with_fact, LIBRARY_TEXT)
    if v1.kind is not VerdictKind.ACCEPT:
        return CheckResult(
            10,
            "черновик: источники + has_gaps",
            CheckKind.AUTO,
            False,
            f"сценарий с фактом в библиотеке дал {v1.kind.value}, ожидался accept",
        )

    gap_declared = DraftCandidate(
        session_id="s1",
        trigger_segment_id="seg2",
        draft_ru="Точной цены годовой подписки нет данных, но месячная — 4900 рублей.",
        target_language="en",
        sources=("Тариф Pro стоит 4900 рублей в месяц.",),
        has_gaps_claimed=True,
        gap_note="нет данных о годовой подписке",
    )
    v2 = guard.verify(gap_declared, LIBRARY_TEXT)
    if v2.kind is not VerdictKind.ACCEPT_WITH_GAPS:
        return CheckResult(
            10,
            "черновик: источники + has_gaps",
            CheckKind.AUTO,
            False,
            f"сценарий с заявленным пробелом дал {v2.kind.value}, ожидался accept_gaps",
        )

    gap_hidden = DraftCandidate(
        session_id="s1",
        trigger_segment_id="seg3",
        draft_ru="Годовая подписка стоит 45000 рублей.",
        target_language="en",
        sources=(),
        has_gaps_claimed=False,
        gap_note=None,
    )
    v3 = guard.verify(gap_hidden, LIBRARY_TEXT)
    if v3.kind is not VerdictKind.REJECT:
        return CheckResult(
            10,
            "черновик: источники + has_gaps",
            CheckKind.AUTO,
            False,
            f"выдуманное число без пометки has_gaps прошло как {v3.kind.value}, ожидался reject",
        )

    return CheckResult(
        10,
        "черновик: источники + has_gaps",
        CheckKind.AUTO,
        True,
        "accept с фактом из библиотеки, accept_gaps при заявленном пробеле, "
        "reject при невыдуманном числе без пометки — все три сценария верны",
    )


CHECK = CheckDef(
    number=10, title="черновик: источники и has_gaps при нехватке", kind=CheckKind.AUTO, run=_run
)
