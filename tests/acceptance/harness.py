"""tests/acceptance/harness.py — H2: движок приёмочного стенда (§21).

Каждая проверка изолирована: своя временная директория, своя SQLite,
свои фикстуры. Провал одной не останавливает прогон. LIVE-проверки без
--live помечаются passed=None, а не пропускаются молча.
"""

from __future__ import annotations

import importlib
import pkgutil
import shutil
import tempfile
import traceback
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

from app.db import Database, DbConfig

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"

#: Канареечный ключ для критерия 15. Не настоящий секрет — маркер утечки.
CANARY_KEY = "sk-CANARY-12345-do-not-use-in-prod"


class CheckKind(str, Enum):
    AUTO = "auto"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class CheckResult:
    number: int
    title: str
    kind: CheckKind
    passed: bool | None  # None = не выполнялось
    detail: str
    evidence_path: Path | None = None


@dataclass
class CheckEnv:
    """Изолированное окружение одной проверки: своя БД, своя директория."""

    tmp_dir: Path
    db: Database

    @property
    def artifacts_dir(self) -> Path:
        d = self.tmp_dir / "artifacts"
        d.mkdir(exist_ok=True)
        return d


CheckFn = Callable[[CheckEnv], Awaitable[CheckResult]]


@dataclass(frozen=True, slots=True)
class CheckDef:
    number: int
    title: str
    kind: CheckKind
    run: CheckFn | None = None  # None для LIVE-проверок без автоматизации


def live_stub(number: int, title: str, instruction: str) -> CheckDef:
    """LIVE-критерий без автоматизации: скрипт лишь объясняет, что делать руками."""

    async def _not_run(_env: CheckEnv) -> CheckResult:
        return CheckResult(number, title, CheckKind.LIVE, None, instruction)

    return CheckDef(number, title, CheckKind.LIVE, run=_not_run)


def _discover() -> list[CheckDef]:
    checks: list[CheckDef] = []
    import tests.acceptance as pkg

    for _, name, _ in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
        leaf = name.rsplit(".", 1)[-1]
        if not leaf.startswith("criterion_"):
            continue
        mod = importlib.import_module(name)
        checks.append(mod.CHECK)
    checks.sort(key=lambda c: c.number)
    return checks


async def _new_env(number: int) -> CheckEnv:
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"h2_check_{number:02d}_"))
    db = Database(DbConfig(path=tmp_dir / "speech.db"))
    await db.start()
    await db.migrate(MIGRATIONS_DIR)
    return CheckEnv(tmp_dir=tmp_dir, db=db)


async def run_all(*, include_live: bool = False, only: set[int] | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in _discover():
        if only is not None and check.number not in only:
            continue
        if check.kind is CheckKind.LIVE and not include_live:
            results.append(
                CheckResult(
                    check.number,
                    check.title,
                    check.kind,
                    None,
                    "не выполнялось (LIVE, нужен флаг --live и живое железо)",
                )
            )
            continue

        env = await _new_env(check.number)
        try:
            assert check.run is not None
            result = await check.run(env)
        except Exception as exc:  # noqa: BLE001 — провал проверки, не прогона
            result = CheckResult(
                check.number,
                check.title,
                check.kind,
                False,
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            )
        finally:
            await env.db.close()

        if result.passed is False and result.evidence_path is None:
            result = replace(result, evidence_path=env.tmp_dir)

        if result.evidence_path != env.tmp_dir:
            shutil.rmtree(env.tmp_dir, ignore_errors=True)

        results.append(result)
    return results


def report_markdown(results: list[CheckResult]) -> str:
    lines = [
        f"# Приёмочный отчёт H2 — {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "| № | Критерий | Вид | Вердикт | Детали | Артефакт |",
        "| --: | :-- | :-- | :-- | :-- | :-- |",
    ]
    passed = failed = not_run = 0
    for r in sorted(results, key=lambda r: r.number):
        if r.passed is True:
            verdict = "✅ пройден"
        elif r.passed is False:
            verdict = "❌ провален"
        else:
            verdict = "⏸ не выполнялось"
        if r.passed is True:
            passed += 1
        elif r.passed is False:
            failed += 1
        else:
            not_run += 1
        detail = r.detail.splitlines()[0] if r.detail else ""
        evidence = str(r.evidence_path) if r.evidence_path else "—"
        lines.append(
            f"| {r.number} | {r.title} | {r.kind.value} | {verdict} | {detail} | {evidence} |"
        )
    lines += [
        "",
        f"**Итог: {passed} пройдено / {failed} провалено / {not_run} не выполнялось "
        f"из {len(results)}.**",
    ]
    return "\n".join(lines) + "\n"
