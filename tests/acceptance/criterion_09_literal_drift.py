"""Критерий 9 (§21): live_literal не теряет числа/даты/имена/URL — 20 фрагментов.

AUTO-режим не ходит к реальным провайдерам (H2, пункт 2): здесь проверяется
не перевод "вживую", а то, что алгоритм детекции дрейфа (D4) верно ловит
потерю сущности на ЗАФИКСИРОВАННЫХ парах текст/перевод из
tests/fixtures/drift_20.json. Проверка на живых моделях — отдельная
LIVE-задача владельца (см. docs/CONTRACTS).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.translation.base import TranslationMode, TranslationRequest
from app.translation.prompts import detect_drift
from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "drift_20.json"


async def _run(_env: CheckEnv) -> CheckResult:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    failures: list[str] = []

    for case in cases:
        req = TranslationRequest(
            text=case["text"],
            source_language=case["source_language"],
            target_language=case["target_language"],
            mode=TranslationMode.LIVE_LITERAL,
        )
        changes = detect_drift(req, case["translation"])
        has_drift = any(c.type == "lost_entity" for c in changes)
        if has_drift != case["expect_drift"]:
            failures.append(
                f"case {case['id']}: expect_drift={case['expect_drift']}, got={has_drift}"
            )
            continue
        if case["expect_drift"]:
            lost = {c.original for c in changes if c.type == "lost_entity" and c.original}
            expected = set(case["expect_originals"])
            if lost != expected:
                failures.append(f"case {case['id']}: originals {expected} != {lost}")

    if failures:
        return CheckResult(
            9,
            "live_literal сохраняет факты (20 фрагментов)",
            CheckKind.AUTO,
            False,
            f"{len(failures)}/{len(cases)} провалились: " + "; ".join(failures[:5]),
        )

    return CheckResult(
        9,
        "live_literal сохраняет факты (20 фрагментов)",
        CheckKind.AUTO,
        True,
        f"все {len(cases)} фрагментов drift_20.json классифицированы верно",
    )


CHECK = CheckDef(
    number=9, title="live_literal сохраняет числа/даты/имена/URL", kind=CheckKind.AUTO, run=_run
)
