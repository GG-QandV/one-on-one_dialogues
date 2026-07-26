from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

from app.db import Database, DbConfig
from app.drafts.guardrails import (
    DraftGuard,
    DraftCandidate,
    GuardConfig,
    VerdictKind,
    extract_numbers,
    Verdict,
)
from app.errors import InvariantViolation


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    cfg = DbConfig(path=Path(path))
    database = Database(cfg)
    await database.start()
    mig_dir = Path(__file__).parent.parent / "migrations"
    await database.migrate(mig_dir)
    yield database
    await database.close()
    Path(path).unlink(missing_ok=True)


class TestExtractNumbers:

    def test_extracts_simple_numbers(self):
        result = extract_numbers("цена 30000 евро")
        assert 30000 in result

    def test_extracts_with_suffix(self):
        result = extract_numbers("бюджет 1.5м рублей")
        assert 1_500_000 in result

    def test_empty_text_returns_empty(self):
        assert extract_numbers("") == set()


class TestDraftGuard:

    def test_rejects_unverified_numbers_strict(self):
        guard = DraftGuard.__new__(DraftGuard)
        guard._cfg = GuardConfig(strict_numbers=True)

        candidate = DraftCandidate(
            session_id="s1",
            trigger_segment_id="seg1",
            draft_ru="цена 50000 рублей",
            target_language="ru",
            sources=("lib1",),
            has_gaps_claimed=False,
            gap_note=None,
        )
        verdict = guard.verify(candidate, "стандартная цена 30000 рублей")
        assert verdict.kind is VerdictKind.REJECT
        assert len(verdict.unverified_numbers) > 0

    def test_accepts_when_numbers_in_library(self):
        guard = DraftGuard.__new__(DraftGuard)
        guard._cfg = GuardConfig(strict_numbers=False)

        candidate = DraftCandidate(
            session_id="s1",
            trigger_segment_id="seg1",
            draft_ru="цена 30000 рублей",
            target_language="ru",
            sources=("lib1",),
            has_gaps_claimed=False,
            gap_note=None,
        )
        verdict = guard.verify(candidate, "стандартная цена 30000 рублей")
        assert verdict.kind is VerdictKind.ACCEPT

    def test_rejects_when_no_sources_and_no_gaps(self):
        guard = DraftGuard.__new__(DraftGuard)
        guard._cfg = GuardConfig(strict_numbers=False)

        candidate = DraftCandidate(
            session_id="s1",
            trigger_segment_id="seg1",
            draft_ru="стандартный ответ",
            target_language="ru",
            sources=(),
            has_gaps_claimed=False,
            gap_note=None,
        )
        verdict = guard.verify(candidate, "")
        assert verdict.kind is VerdictKind.REJECT


class TestDraftIsolation:

    @pytest.mark.asyncio
    async def test_store_writes_only_draft_answers(self, db):
        sid = uuid.uuid4().hex
        seg_id = uuid.uuid4().hex
        stream_id = uuid.uuid4().hex
        now = "2025-01-01T00:00:00.000"

        def _seed(conn):
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, started_at, status, default_privacy_profile, mode) VALUES (?, ?, 'active', 'open', 'live_safe')",
                (sid, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO audio_streams (id, session_id, role, source_language, target_language, enabled) VALUES (?, ?, 'microphone', 'ru', 'en', 1)",
                (stream_id, sid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, privacy_profile, track, created_at) VALUES (?, ?, ?, 0, 1000, 'open', 'accurate', ?)",
                (seg_id, sid, stream_id, now),
            )

        await db.write(_seed)

        guard = DraftGuard(db)
        draft_id = await guard.store(
            DraftCandidate(
                session_id=sid,
                trigger_segment_id=seg_id,
                draft_ru="ответ 42",
                target_language="ru",
                sources=("ctx",),
                has_gaps_claimed=False,
                gap_note=None,
            ),
            Verdict(VerdictKind.ACCEPT, (), ()),
        )
        assert draft_id is not None

        draft = await db.fetch_one(
            "SELECT id, draft_ru FROM draft_answers WHERE id = ?",
            (draft_id,),
        )
        assert draft is not None
        assert draft["draft_ru"] == "ответ 42"


class TestNoAutoDeliver:

    @pytest.mark.asyncio
    async def test_can_mark_ignored_and_copied(self, db):
        guard = DraftGuard(db)
        sid = uuid.uuid4().hex
        seg_id = uuid.uuid4().hex
        stream_id = uuid.uuid4().hex
        now = "2025-01-01T00:00:00.000"

        def _seed(conn):
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, started_at, status, default_privacy_profile, mode) VALUES (?, ?, 'active', 'open', 'live_safe')",
                (sid, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO audio_streams (id, session_id, role, source_language, target_language, enabled) VALUES (?, ?, 'microphone', 'ru', 'en', 1)",
                (stream_id, sid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, privacy_profile, track, created_at) VALUES (?, ?, ?, 0, 1000, 'open', 'accurate', ?)",
                (seg_id, sid, stream_id, now),
            )

        await db.write(_seed)

        draft_id = await guard.store(
            DraftCandidate(
                session_id=sid,
                trigger_segment_id=seg_id,
                draft_ru="ok",
                target_language="ru",
                sources=("ctx",),
                has_gaps_claimed=False,
                gap_note=None,
            ),
            Verdict(VerdictKind.ACCEPT, (), ()),
        )
        assert draft_id is not None

        await guard.mark(draft_id, "ignored")
        row = await db.fetch_one(
            "SELECT status FROM draft_answers WHERE id = ?", (draft_id,)
        )
        assert row["status"] == "ignored"

        await guard.mark(draft_id, "copied")
        row = await db.fetch_one(
            "SELECT status FROM draft_answers WHERE id = ?", (draft_id,)
        )
        assert row["status"] == "copied"
