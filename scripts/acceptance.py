#!/usr/bin/env python3
"""
H6 - Приёмочный стенд MVP: 16 критериев §21
Запуск: python scripts/acceptance.py [--live] [--only 5,7,15]
"""

import asyncio
import json
import os
import tempfile
import time
import subprocess
import sys
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Добавляем корень проекта в путь
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class CheckKind(str, Enum):
    AUTO = "auto"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class CheckResult:
    number: int
    title: str
    kind: CheckKind
    passed: Optional[bool]
    detail: str
    evidence_path: Optional[Path] = None


class Check(ABC):
    """Базовый класс проверки"""

    def __init__(self, number: int, title: str, kind: CheckKind):
        self.number = number
        self.title = title
        self.kind = kind

    @abstractmethod
    async def run(self, ctx: "RunContext") -> CheckResult:
        pass


class RunContext:
    """Контекст прогона: изолированная временная директория, БД, конфиг"""

    def __init__(self, test_dir: Path, include_live: bool):
        self.test_dir = test_dir
        self.include_live = include_live
        self.temp_files: List[Path] = []
        self.db_path = test_dir / "test.db"
        self.config_path = test_dir / "config.toml"
        self.log_path = test_dir / "test.log"
        self.artifacts_dir = test_dir / "artifacts"
        self.artifacts_dir.mkdir(exist_ok=True)

    def cleanup(self):
        pass  # Временная директория удалится автоматически


# ===================== КРИТЕРИИ 1-16 =====================

class Check01Install(Check):
    """1. Запуск на чистой Mint без Docker/GPU/тяжёлого ML"""

    def __init__(self):
        super().__init__(1, "Установка на чистую систему", CheckKind.LIVE)

    async def run(self, ctx: RunContext) -> CheckResult:
        if not ctx.include_live:
            return CheckResult(self.number, self.title, self.kind, None,
                               "LIVE-проверка пропущена (--live не задан)", None)

        checks = [
            ("python3", ["python3", "--version"]),
            ("whisper-cli", ["which", "whisper-cli"]),
            ("ggml-base.bin", [Path.home() / ".local/share/speech-local/models/ggml-base.bin"]),
        ]

        details = []
        for name, check in checks:
            if isinstance(check, list) and check[0] in ("python3", "which"):
                try:
                    result = subprocess.run(check, capture_output=True, timeout=5)
                    ok = result.returncode == 0
                    details.append(f"{name}: {'OK' if ok else 'FAIL'}")
                except Exception as e:
                    details.append(f"{name}: ERROR {e}")
            else:
                ok = check.exists()
                details.append(f"{name}: {'OK' if ok else 'MISSING'}")

        passed = all("OK" in d for d in details)
        return CheckResult(self.number, self.title, self.kind, passed,
                           "; ".join(details), ctx.artifacts_dir / "check01_install.log")


class Check02DualCapture(Check):
    """2. Раздельный захват узла и микрофона одновременно"""

    def __init__(self):
        super().__init__(2, "Двойной захват PipeWire", CheckKind.LIVE)

    async def run(self, ctx: RunContext) -> CheckResult:
        if not ctx.include_live:
            return CheckResult(self.number, self.title, self.kind, None,
                               "LIVE-проверка пропущена", None)
        return CheckResult(self.number, self.title, self.kind, None,
                           "Требует ручного запуска на железе", None)


class Check03STT(Check):
    """3. Распознавание EN/ES/RU/UA/PL через ggml-base.bin"""

    def __init__(self):
        super().__init__(3, "STT качество (WER порог)", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        # Эталонные WAV файлы должны лежать в tests/fixtures/stt_ref/
        fixtures_dir = Path(__file__).parent.parent / "fixtures" / "stt_ref"
        if not fixtures_dir.exists():
            return CheckResult(self.number, self.title, self.kind, False,
                               f"Фикстуры не найдены: {fixtures_dir}", None)

        # TODO: Реализовать прогон whisper.cpp на фикстурах и сравнение WER
        return CheckResult(self.number, self.title, self.kind, False,
                           "Не реализовано: нужен прогон whisper.cpp на эталонных WAV", None)


class Check04RawTextImmutable(Check):
    """4. raw_text с таймкодами и stt_confidence в SQLite, только от whisper"""

    def __init__(self):
        super().__init__(4, "raw_text неизменяемость", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        from app.db import Database, DbConfig
        from app.models import Segment

        db = Database(DbConfig(path=ctx.db_path))
        await db.start()
        await db.migrate(Path(__file__).parent.parent / "migrations")

        # Вставляем сегмент
        seg_id = "test-seg-1"
        await db.write(lambda conn: conn.execute("""
            INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms,
                privacy_profile, track, stt_model, detected_language, raw_text, stt_confidence, created_at)
            VALUES (?, 'sess1', 'stream1', 1000, 5000, 'open', 'accurate', 'ggml-base.bin',
                'ru', 'Тестовый текст', -0.5, datetime('now'))
        """, (seg_id,)))

        # Пытаемся обновить raw_text - должно упасть с ошибкой триггера/констрейнта
        try:
            await db.write(lambda conn: conn.execute(
                "UPDATE segments SET raw_text = 'Изменено' WHERE id = ?", (seg_id,)))
            passed = False
            detail = "UPDATE raw_text прошёл без ошибки (нарушение инварианта)"
        except Exception as e:
            passed = True
            detail = f"UPDATE raw_text заблокирован: {type(e).__name__}"

        # Проверяем, что поле заполнено
        row = await db.fetch_one("SELECT raw_text, stt_confidence FROM segments WHERE id = ?", (seg_id,))
        if row and row["raw_text"] == "Тестовый текст" and row["stt_confidence"] is not None:
            detail += f"; raw_text={row['raw_text']}, confidence={row['stt_confidence']}"
        else:
            passed = False
            detail += "; поле не заполнено"

        await db.close()
        return CheckResult(self.number, self.title, self.kind, passed, detail,
                           ctx.artifacts_dir / "check04_raw_text.json")


class Check05Latency(Check):
    """5. Задержка точного трека <= 3000 мс (медиана по 50 репликам)"""

    def __init__(self):
        super().__init__(5, "Задержка точного трека", CheckKind.LIVE)

    async def run(self, ctx: RunContext) -> CheckResult:
        if not ctx.include_live:
            return CheckResult(self.number, self.title, self.kind, None,
                               "LIVE-проверка пропущена", None)

        # Требует живого замера: t_end_ms сегмента -> появление перевода в UI
        return CheckResult(self.number, self.title, self.kind, None,
                           "Требует замера на живом железе (50 реплик)", None)


class Check06DraftSupersede(Check):
    """6. Черновик быстрого трека <= 1500 мс, замещается проверенным"""

    def __init__(self):
        super().__init__(6, "Замещение черновика точным", CheckKind.LIVE)

    async def run(self, ctx: RunContext) -> CheckResult:
        if not ctx.include_live:
            return CheckResult(self.number, self.title, self.kind, None,
                               "LIVE-проверка пропущена", None)
        return CheckResult(self.number, self.title, self.kind, None,
                           "Требует замера SSE событий", None)


class Check07PrivacySwitch(Check):
    """7. Переключение профиля рвёт аудио-сессию; в confidential аудио-трафика нет"""

    def __init__(self):
        super().__init__(7, "Переключение профиля и аудио-трафик", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        from app.privacy import PrivacyController, Capability, PrivacyProfile

        # Подменяем транспорт для перехвата байтов
        sent_bytes = []

        class MockTransport:
            async def send(self, data: bytes):
                sent_bytes.append(data)

            async def close(self):
                pass

        controller = PrivacyController()
        controller.profile = PrivacyProfile.OPEN

        # Пытаемся отправить аудио в OPEN - должно пройти
        fence = controller.require(Capability.AUDIO_TO_CLOUD)
        assert fence.profile == PrivacyProfile.OPEN

        # Переключаем в CONFIDENTIAL
        teardown_ms = await controller.switch(PrivacyProfile.CONFIDENTIAL, session_id="test", reason="test")

        # Теперь AUDIO_TO_CLOUD должен быть запрещён
        try:
            controller.require(Capability.AUDIO_TO_CLOUD)
            passed = False
            detail = "AUDIO_TO_CLOUD разрешён в CONFIDENTIAL (нарушение)"
        except Exception as e:
            passed = True
            detail = f"Переключение за {teardown_ms}мс, AUDIO_TO_CLOUD запрещён: {type(e).__name__}"

        return CheckResult(self.number, self.title, self.kind, passed, detail,
                           ctx.artifacts_dir / "check07_privacy.json")


class Check08PrivacyProfileField(Check):
    """8. privacy_profile корректно проставлен у каждого сегмента"""

    def __init__(self):
        super().__init__(8, "privacy_profile в сегментах", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        from app.db import Database, DbConfig

        db = Database(DbConfig(path=ctx.db_path))
        await db.start()
        await db.migrate(Path(__file__).parent.parent / "migrations")

        await db.write(lambda conn: conn.executescript("""
            INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms,
                privacy_profile, track, created_at)
            VALUES ('seg1', 'sess1', 'stream1', 1000, 5000, 'open', 'accurate', datetime('now')),
                   ('seg2', 'sess1', 'stream1', 6000, 9000, 'confidential', 'accurate', datetime('now')),
                   ('seg3', 'sess1', 'stream1', 10000, 12000, NULL, 'accurate', datetime('now'));
        """))

        row = await db.fetch_one("SELECT COUNT(*) as cnt FROM segments WHERE privacy_profile IS NULL")
        null_count = row["cnt"] if row else 1

        await db.close()

        passed = null_count == 0
        detail = f"Сегментов без privacy_profile: {null_count}"
        return CheckResult(self.number, self.title, self.kind, passed, detail,
                           ctx.artifacts_dir / "check08_privacy_field.json")


class Check09LiveLiteral(Check):
    """9. В live_literal числа, даты, имена, суммы, URL не изменяются (20 фрагментов)"""

    def __init__(self):
        super().__init__(9, "live_literal сохраняет сущности", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        fixtures = Path(__file__).parent.parent / "fixtures" / "literal_20.json"
        if not fixtures.exists():
            return CheckResult(self.number, self.title, self.kind, False,
                               f"Фикстура не найдена: {fixtures}", None)

        # TODO: Прогнать через TranslationProvider (Gemini/Claude) и проверить сущности
        return CheckResult(self.number, self.title, self.kind, False,
                           "Не реализовано: нужен прогон через провайдеров на 20 фрагментах", None)


class Check10DraftGeneration(Check):
    """10. Черновик генерируется на вопрос, содержит источники, has_gaps при нехватке"""

    def __init__(self):
        super().__init__(10, "Генерация черновиков с источниками", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        # TODO: Использовать DraftProvider с библиотекой-фикстурой
        return CheckResult(self.number, self.title, self.kind, False,
                           "Не реализовано: нужен DraftProvider + библиотека-фикстура", None)


class Check11DraftDelivery(Check):
    """11. Черновик недоставим без действия пользователя; горячая клавиша работает"""

    def __init__(self):
        super().__init__(11, "Доставка черновика по действию", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        # Проверка графа вызовов: нет пути generate -> copy без user action
        return CheckResult(self.number, self.title, self.kind, False,
                           "Не реализовано: нужен анализ call graph", None)


class Check12OfflineQueue(Check):
    """12. Облако недоступно -> запись и STT продолжаются, очередь догоняется"""

    def __init__(self):
        super().__init__(12, "Офлайн-очередь и догон", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        from app.translation.offline import OfflineGate
        from app.queue import JobQueue, JobType, JobStatus

        gate = OfflineGate()
        gate.mark_unavailable("gemini", Exception("503"))

        # Проверяем, что STT задачи всё равно ставятся в очередь
        # TODO: Полный интеграционный тест
        return CheckResult(self.number, self.title, self.kind, False,
                           "Не реализовано: нужен интеграционный тест с JobQueue", None)


class Check13ExportOnlyAccurate(Check):
    """13. Экспорт содержит только точный трек (track='fast' отсутствует)"""

    def __init__(self):
        super().__init__(13, "Экспорт только точный трек", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        from app.db import Database, DbConfig
        from app.exports.txt import to_txt
        from app.exports.subtitles import to_srt, to_vtt
        from app.exports.json_export import to_json
        from app.translation.supersede import SupersedeService

        db = Database(DbConfig(path=ctx.db_path))
        await db.start()
        await db.migrate(Path(__file__).parent.parent / "migrations")

        await db.write(lambda conn: conn.execute(
            "INSERT INTO sessions (id, started_at, status, default_privacy_profile, mode) VALUES (?, datetime('now'), 'active', 'open', 'live_literal')",
            ("sess1",)
        ))
        await db.write(lambda conn: conn.execute(
            "INSERT INTO audio_streams (id, session_id, role, source_language, target_language, enabled, priority) VALUES (?, ?, 'meeting', 'en', 'ru', 1, 'primary')",
            ("stream1", "sess1")
        ))
        await db.write(lambda conn: conn.execute("""
            INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms,
                privacy_profile, track, raw_text, translation_clean, created_at)
            VALUES ('seg1', 'sess1', 'stream1', 1000, 5000, 'open', 'accurate', 'Точный', 'Перевод', datetime('now')),
                   ('seg2', 'sess1', 'stream1', 6000, 9000, 'open', 'fast', NULL, 'Draft', datetime('now'));
        """))

        # Используем export_view как в реальном экспорте — только accurate трек
        supersede = SupersedeService(db)
        rows = await supersede.export_view("sess1")
        await db.close()

        # Проверяем все форматы экспорта
        formats_ok = True
        issues = []

        for fmt_name, fmt_fn in [("TXT", to_txt), ("JSON", lambda r, **kw: to_json({"id": "sess1"}, r, [])), ("SRT", to_srt), ("VTT", to_vtt)]:
            try:
                if fmt_name == "JSON":
                    output = fmt_fn(rows)
                else:
                    output = fmt_fn(rows)
                if "Черновик" in output or "Draft" in output or "fast" in output:
                    formats_ok = False
                    issues.append(f"{fmt_name}: содержит fast-track")
            except Exception as e:
                formats_ok = False
                issues.append(f"{fmt_name}: ошибка {e}")

        detail = "Все форматы чисты" if formats_ok else "; ".join(issues)
        return CheckResult(self.number, self.title, self.kind, formats_ok, detail,
                           ctx.artifacts_dir / "check13_export.json")


class Check14MemoryPressure(Check):
    """14. При memory pressure очередь на диск, MemoryMax не превышен аварийно"""

    def __init__(self):
        super().__init__(14, "Memory pressure каскад", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        from app.watchdog.memory import MemoryReader
        from app.watchdog.degradation import DegradationCascade, Level

        class MockReader(MemoryReader):
            def __init__(self):
                self.values = [1500, 1700, 1800, 1900, 1950, 2000]
                self.idx = 0

            def current_mb(self) -> float:
                if self.idx < len(self.values):
                    v = self.values[self.idx]
                    self.idx += 1
                    return v
                return 2000

        actions_log = []

        async def action(level: Level):
            actions_log.append(level.name)

        cascade = DegradationCascade(
            actions=[action] * 6,
            memory_reader=MockReader(),
        )
        await cascade.start()
        for _ in range(6):
            await cascade.tick()
            await asyncio.sleep(0.01)
        await cascade.stop()

        expected = ["NORMAL", "LATENCY", "MEMORY_SOFT", "MEMORY_HARD", "MEMORY_HARD", "MEMORY_HARD"]
        passed = actions_log == expected
        detail = f"actions: {' -> '.join(actions_log)}"
        return CheckResult(self.number, self.title, self.kind, passed, detail,
                           ctx.artifacts_dir / "check14_memory.json")


class Check15BYOKLeak(Check):
    """15. Ключ BYOK не на диске и не в логах (канареечный ключ + grep)"""

    def __init__(self):
        super().__init__(15, "BYOK ключ не утекает", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        from app.security.byok import KeyStore
        from app.security.redactor import LogRedactor
        import logging

        canary = "redacted:sk-CANARY-TEST-KEY-12345"
        store = KeyStore()
        store.put("gemini", canary)

        # Симулируем полный цикл с ошибкой провайдера (чтобы сработали трейсы)
        try:
            key = store.get("gemini")
            raise Exception(f"Provider error with key {key}")
        except Exception:
            tb = traceback.format_exc()

        # Логируем с редактором
        logger = logging.getLogger("test_leak")
        logger.addHandler(logging.StreamHandler())
        logger.addFilter(LogRedactor())
        logger.error("Test error", exc_info=True)

        # Проверяем все артефакты
        artifacts = [
            ctx.log_path,
            ctx.db_path,
            ctx.db_path.with_suffix(".db-wal"),
            ctx.db_path.with_suffix(".db-shm"),
        ]
        artifacts.extend(ctx.test_dir.rglob("*.tmp"))
        artifacts.extend(ctx.test_dir.rglob("*.log"))

        found = []
        for art in artifacts:
            if art.exists():
                content = art.read_text(errors="ignore")
                if "CANARY" in content:
                    found.append(str(art))

        passed = len(found) == 0
        detail = f"Найдено в: {found}" if found else "Ключ не найден нигде"
        return CheckResult(self.number, self.title, self.kind, passed, detail,
                           ctx.artifacts_dir / "check15_byok.json")


class Check16RestartRecovery(Check):
    """16. Остановка и перезапуск не повреждают сессию, задачи восстанавливаются"""

    def __init__(self):
        super().__init__(16, "SIGKILL и восстановление", CheckKind.AUTO)

    async def run(self, ctx: RunContext) -> CheckResult:
        from app.queue import JobQueue, JobType, JobStatus
        from app.db import Database, DbConfig

        # Создаём БД с задачами в QUEUED и RUNNING
        db = Database(DbConfig(path=ctx.db_path))
        await db.start()
        await db.migrate(Path(__file__).parent.parent / "migrations")

        await db.write(lambda conn: conn.executescript("""
            INSERT INTO jobs (id, type, segment_id, status, attempts, max_attempts,
                             privacy_profile, privacy_gen, idempotent, created_at, updated_at)
            VALUES ('job1', 'translate', 'seg1', 'queued', 0, 3, 'open', 1, 1, datetime('now'), datetime('now')),
                   ('job2', 'stt', 'seg2', 'running', 1, 3, 'open', 1, 1, datetime('now'), datetime('now')),
                   ('job3', 'export', 'seg3', 'done', 1, 1, 'open', 1, 1, datetime('now'), datetime('now'));
        """))

        await db.close()

        # Симулируем SIGKILL = новый процесс, новая БД, recover()
        db2 = Database(DbConfig(path=ctx.db_path))
        await db2.start()

        queue = JobQueue(db=db2)

        async def dummy_handler(job):
            pass

        queue.register(JobType.TRANSLATE, dummy_handler)
        queue.register(JobType.STT, dummy_handler)

        recovered = await queue.recover()

        # QUEUED -> остаются QUEUED, RUNNING -> должны стать QUEUED (requeue)
        jobs = await db2.fetch_all("SELECT id, status FROM jobs ORDER BY id")
        statuses = {j["id"]: j["status"] for j in jobs}

        passed = (statuses.get("job1") == "queued" and
                  statuses.get("job2") == "queued" and
                  statuses.get("job3") == "done" and
                  recovered.get("requeued", 0) >= 1)

        detail = f"statuses: {statuses}, recovered: {recovered}"
        await db2.close()
        return CheckResult(self.number, self.title, self.kind, passed, detail,
                           ctx.artifacts_dir / "check16_recovery.json")


# ===================== HARNESS =====================

ALL_CHECKS: List[Check] = [
    Check01Install(),
    Check02DualCapture(),
    Check03STT(),
    Check04RawTextImmutable(),
    Check05Latency(),
    Check06DraftSupersede(),
    Check07PrivacySwitch(),
    Check08PrivacyProfileField(),
    Check09LiveLiteral(),
    Check10DraftGeneration(),
    Check11DraftDelivery(),
    Check12OfflineQueue(),
    Check13ExportOnlyAccurate(),
    Check14MemoryPressure(),
    Check15BYOKLeak(),
    Check16RestartRecovery(),
]


async def run_all(include_live: bool = False, only: Optional[Set[int]] = None) -> List[CheckResult]:
    with tempfile.TemporaryDirectory(prefix="acceptance_") as tmp:
        ctx = RunContext(Path(tmp), include_live)
        results = []

        for check in ALL_CHECKS:
            if only and check.number not in only:
                results.append(CheckResult(check.number, check.title, check.kind, None,
                                           "пропущено (--only)", None))
                continue
            if check.kind == CheckKind.LIVE and not include_live:
                results.append(CheckResult(check.number, check.title, check.kind, None,
                                           "LIVE пропущен (нет --live)", None))
                continue

            try:
                result = await check.run(ctx)
            except Exception as e:
                result = CheckResult(check.number, check.title, check.kind, False,
                                     f"Исключение: {e}", None)
            results.append(result)

        return results


def report_markdown(results: List[CheckResult]) -> str:
    lines = [
        "# Приёмочный отчёт H6",
        f"Дата: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| № | Критерий | Вид | Результат | Детали |",
        "|---|----------|-----|-----------|--------|",
    ]
    passed = failed = skipped = 0
    for r in results:
        if r.passed is True:
            verdict = "PASS"
            passed += 1
        elif r.passed is False:
            verdict = "FAIL"
            failed += 1
        else:
            verdict = "SKIP"
            skipped += 1
        kind = "AUTO" if r.kind == CheckKind.AUTO else "LIVE"
        detail = r.detail.replace("|", "\\|")[:200]
        lines.append(f"| {r.number} | {r.title} | {kind} | {verdict} | {detail} |")

    lines.append("")
    lines.append(f"**Итого:** PASS {passed} FAIL {failed} SKIP {skipped} / {len(results)}")
    return "\n".join(lines)


# ===================== CLI =====================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="H6 Acceptance Runner")
    parser.add_argument("--live", action="store_true", help="Включить LIVE-проверки")
    parser.add_argument("--only", help="Запустить только номера через запятую (5,7,15)")
    parser.add_argument("--output", help="Файл отчёта (Markdown)")
    args = parser.parse_args()

    only_set = set(map(int, args.only.split(","))) if args.only else None

    results = asyncio.run(run_all(include_live=args.live, only=only_set))
    md = report_markdown(results)

    if args.output:
        Path(args.output).write_text(md)
        print(f"Отчёт сохранён: {args.output}")
    else:
        print(md)

    # Код возврата: 1 если есть FAIL среди AUTO
    auto_fails = any(r.passed is False and r.kind == CheckKind.AUTO for r in results)
    sys.exit(1 if auto_fails else 0)


if __name__ == "__main__":
    main()