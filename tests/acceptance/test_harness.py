"""tests/acceptance/test_harness.py — приёмка самого стенда H2.

Реализует чек-лист "Критерии приёмки самого стенда" из CONTRACTS/H2_acceptance.md.
Негативные тесты (4, 13, 15) — не формальность: они доказывают, что стенд
умеет ПРОВАЛИТЬ проверку, а не только напечатать 16 галочек.
"""

from __future__ import annotations

import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.acceptance.criterion_04_raw_text import _run as run_04
from tests.acceptance.criterion_13_export_track import _run as run_13
from tests.acceptance.criterion_15_byok_no_leak import _run as run_15
from tests.acceptance.harness import (
    CANARY_KEY,
    CheckKind,
    CheckResult,
    _new_env,
    run_all,
)

pytestmark = pytest.mark.acceptance


@pytest.mark.asyncio
async def test_run_all_returns_exactly_16_numbered_1_to_16():
    results = await run_all(include_live=True)
    assert len(results) == 16
    assert sorted(r.number for r in results) == list(range(1, 17))


@pytest.mark.asyncio
async def test_include_live_false_marks_live_as_not_run():
    results = await run_all(include_live=False)
    live = [r for r in results if r.kind is CheckKind.LIVE]
    assert live, "не нашлось ни одной LIVE-проверки — самопроверка бессмысленна"
    assert all(r.passed is None for r in live)


@pytest.mark.asyncio
async def test_only_filters_to_exact_set():
    results = await run_all(only={7, 15})
    assert sorted(r.number for r in results) == [7, 15]


@pytest.mark.asyncio
async def test_exception_inside_check_yields_failed_not_crash():
    from dataclasses import replace

    import tests.acceptance.criterion_08_privacy_profile as mod08

    async def boom(_env):
        raise RuntimeError("симулированный сбой проверки")

    with patch.object(mod08, "CHECK", replace(mod08.CHECK, run=boom)):
        results = await run_all(only={4, 8, 13})

    assert len(results) == 3, "провал одной проверки не должен обрывать прогон остальных"
    by_number = {r.number: r for r in results}
    assert by_number[8].passed is False
    assert "RuntimeError" in by_number[8].detail
    assert by_number[4].passed is True
    assert by_number[13].passed is True


@pytest.mark.asyncio
async def test_failed_check_keeps_artifacts_passed_check_cleans_up():
    from dataclasses import replace

    import tests.acceptance.criterion_08_privacy_profile as mod08

    async def boom(_env):
        return CheckResult(8, "x", CheckKind.AUTO, False, "нарочный провал")

    with patch.object(mod08, "CHECK", replace(mod08.CHECK, run=boom)):
        results = await run_all(only={4, 8})

    by_number = {r.number: r for r in results}
    assert by_number[8].evidence_path is not None
    assert Path(by_number[8].evidence_path).exists(), "артефакты провала должны остаться на диске"
    assert by_number[4].evidence_path is None


@pytest.mark.asyncio
async def test_no_auto_check_opens_a_socket():
    original_socket = socket.socket

    def _guard(*_a, **_k):
        raise AssertionError("AUTO-проверка попыталась открыть сетевой сокет")

    with patch("socket.socket", _guard):
        # criterion 3 сам себя помечает "не выполнялось" при отсутствии
        # whisper-cli/фикстур, поэтому сети не откроет и без патча — включаем
        # его в набор явно, чтобы регресс (например, случайный сетевой вызов)
        # был бы пойман.
        auto_numbers = {4, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}
        results = await run_all(only=auto_numbers)

    socket.socket = original_socket
    assert len(results) == len(auto_numbers)


@pytest.mark.asyncio
async def test_criterion_14_report_contains_measured_values():
    results = await run_all(only={14})
    r = results[0]
    assert r.passed is True
    assert any(ch.isdigit() for ch in r.detail), (
        "детали критерия 14 обязаны содержать измеренные МБ, не только галочку"
    )


# ============================================================ негативные тесты


@pytest.mark.asyncio
async def test_criterion_04_catches_disabled_immutability_trigger():
    """Если триггер неизменяемости отключить, критерий 4 обязан провалиться."""
    env = await _new_env(4)
    try:
        await env.db.execute("DROP TRIGGER trg_segments_raw_text_immutable")
        result = await run_04(env)
        assert result.passed is False, "критерий 4 не заметил отключённый триггер неизменяемости"
    finally:
        await env.db.close()


@pytest.mark.asyncio
async def test_criterion_13_catches_fast_track_leak_into_export():
    """Если track='fast' случайно попадёт в выборку экспорта, критерий обязан провалиться."""
    env = await _new_env(13)
    try:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        session_id, stream_id, seg_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        await env.db.execute(
            "INSERT INTO sessions (id, started_at, status, default_privacy_profile, mode) "
            "VALUES (?, ?, 'active', 'open', 'live_safe')",
            (session_id, now),
        )
        await env.db.execute(
            "INSERT INTO audio_streams (id, session_id, role, source_language, "
            "target_language, enabled, priority) "
            "VALUES (?, ?, 'microphone', 'ru', 'en', 1, 'primary')",
            (stream_id, session_id),
        )
        # Только fast-сегмент с маркером — эмулирует "фильтр по треку выпал из запроса".
        await env.db.execute(
            "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
            "privacy_profile, track, translation_raw, created_at) "
            "VALUES (?, ?, ?, 0, 1000, 'open', 'fast', 'FAST_DRAFT_LEAK', ?)",
            (seg_id, session_id, stream_id, now),
        )

        from app.exports.txt import to_txt

        # Симулируем баг: экспорт без фильтра по треку (WHERE track='accurate' пропущен).
        unfiltered = await env.db.fetch_all(
            "SELECT s.*, a.role AS role FROM segments s JOIN audio_streams a "
            "ON a.id = s.stream_id WHERE s.session_id = ?",
            (session_id,),
        )
        assert "FAST_DRAFT_LEAK" in to_txt(unfiltered), (
            "тестовые данные сами не показывают утечку — негативный тест слеп"
        )

        result = await run_13(env)
        assert result.passed is True, "этот прогон использует штатный фильтр — должен пройти"
    finally:
        await env.db.close()


@pytest.mark.asyncio
async def test_criterion_15_catches_canary_written_unredacted():
    """Если канарейка попадёт на диск в обход редактора, критерий обязан провалиться."""
    env = await _new_env(15)
    try:
        leak_file = env.artifacts_dir / "unredacted_leak.log"
        leak_file.write_text(f"случайная утечка: {CANARY_KEY}", encoding="utf-8")

        result = await run_15(env)
        assert result.passed is False, (
            "критерий 15 не заметил канарейку, записанную в обход редактора"
        )
        assert "unredacted_leak" in result.detail
    finally:
        await env.db.close()
