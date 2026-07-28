"""Tests for DRAFT handler (app/main.py _handle_draft)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from app.db import Database, DbConfig
from app.drafts.guardrails import DraftCandidate, DraftGuard, GuardConfig, VerdictKind
from app.drafts.provider import DraftProvider, DraftProviderConfig
from app.errors import ProviderError, ProviderUnavailable, StaleGenerationError
from app.main import Application, AppConfig
from app.privacy import PrivacyController, PrivacyProfile
from app.queue import Job, JobStatus, JobType
from app.translation.offline import OfflineGate


class FakeProvider:
    name = "fake"

    async def translate(self, req, *, fence):
        from app.translation.base import TranslationResult
        return TranslationResult(
            translation_raw="translated",
            translation_clean="translated",
        )

    async def close(self):
        pass


class FakeDraftI2:
    """Fake I2 generator that returns a fixed candidate."""

    def __init__(self, succeed: bool = True, candidate: DraftCandidate | None = None):
        self.succeed = succeed
        self._candidate = candidate
        self.last_req = None

    async def generate(self, req, *, fence):
        self.last_req = req
        if not self.succeed:
            raise ProviderUnavailable("fake unavailable")
        if self._candidate is not None:
            return self._candidate
        return DraftCandidate(
            session_id=req.session_id,
            trigger_segment_id=req.trigger_segment_id,
            draft_ru="Ответ на русском",
            target_language="ru",
            sources=("fact1",),
            has_gaps_claimed=False,
            gap_note=None,
        )

    def snapshot(self):
        return {}


class RaiseStaleI2:
    async def generate(self, req, *, fence):
        raise StaleGenerationError()

    def snapshot(self):
        return {}


class AcceptingGuard:
    """Guard that accepts everything."""

    def __init__(self):
        self.last_candidate = None

    def verify(self, candidate, library_text):
        self.last_candidate = candidate
        from app.drafts.guardrails import Verdict, VerdictKind
        return Verdict(VerdictKind.ACCEPT, (), ())

    async def store(self, candidate, verdict):
        return uuid.uuid4().hex


class RejectingGuard:
    """Guard that rejects everything."""

    def verify(self, candidate, library_text):
        from app.drafts.guardrails import Verdict, VerdictKind
        return Verdict(VerdictKind.REJECT, (), ("rejected",))

    async def store(self, candidate, verdict):
        return None


class FakeLibrary:
    def __init__(self, content: str = "факт 100"):
        self.content = content

    async def get(self, context_id):
        from app.drafts.library import LibraryContext
        return LibraryContext(
            id=context_id, name="test", domain=None,
            content_text=self.content,
            token_estimate=10, updated_at="2025-01-01T00:00:00",
        )


def _make_job(segment_id: str, profile=PrivacyProfile.OPEN, gen=0) -> Job:
    return Job(
        id=uuid.uuid4().hex,
        type=JobType.DRAFT,
        segment_id=segment_id,
        payload={},
        status=JobStatus.QUEUED,
        idempotent=True,
        attempts=0,
        max_attempts=3,
        privacy_profile=profile,
        privacy_gen=gen,
        lease_owner=None,
        error_code=None,
    )


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


async def _seed_meeting_segment(db, *, tgt_lang="ru"):
    """Create session + meeting stream + segment; return (session_id, stream_id, segment_id)."""
    sid = uuid.uuid4().hex
    stream_id = uuid.uuid4().hex
    seg_id = uuid.uuid4().hex
    now = "2025-01-01T00:00:00.000"
    lib_id = uuid.uuid4().hex

    await db.execute(
        "INSERT INTO library_contexts (id, name, domain, content_text, token_estimate, updated_at) "
        "VALUES (?, ?, ?, ?, 10, ?)",
        (lib_id, "test", None, "факт 100", now),
    )
    await db.execute(
        "INSERT INTO sessions (id, started_at, status, default_privacy_profile, mode, library_context_id) "
        "VALUES (?, ?, 'active', 'open', 'live_safe', ?)",
        (sid, now, lib_id),
    )
    await db.execute(
        "INSERT INTO audio_streams (id, session_id, role, source_language, target_language, enabled) "
        "VALUES (?, ?, 'meeting', 'en', ?, 1)",
        (stream_id, sid, tgt_lang),
    )
    await db.execute(
        "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
        "privacy_profile, track, raw_text, translation_status, created_at) "
        "VALUES (?, ?, ?, 0, 1000, 'open', 'accurate', 'How much?', 'pending', ?)",
        (seg_id, sid, stream_id, now),
    )
    return sid, stream_id, seg_id


# --------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_draft_positive_flow(db):
    """Meeting question → I2 returns candidate → guard ACCEPT → stored in draft_answers."""
    sid, _, seg_id = await _seed_meeting_segment(db)
    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app._provider = FakeProvider()
    app._draft_provider = FakeDraftI2(succeed=True)
    from app.drafts.guardrails import DraftGuard
    app._draft_guard = DraftGuard(db)
    app._library = FakeLibrary()
    app._session_id = sid

    from app.drafts.translate import DraftTranslator
    app._draft_translator = DraftTranslator(app._provider, DraftGuard(db))
    app.jobs = None

    await app._handle_draft(_make_job(seg_id))

    row = await db.fetch_one(
        "SELECT draft_ru FROM draft_answers WHERE trigger_segment_id = ?",
        (seg_id,),
    )
    assert row is not None
    assert "Ответ на русском" in row["draft_ru"]


@pytest.mark.asyncio
async def test_draft_created_event_payload(db):
    """DRAFT_CREATED событие содержит все служебные поля."""
    sid, _, seg_id = await _seed_meeting_segment(db)
    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app._provider = FakeProvider()
    app._draft_provider = FakeDraftI2(succeed=True)
    from app.drafts.guardrails import DraftGuard
    app._draft_guard = DraftGuard(db)
    app._library = FakeLibrary()
    app._session_id = sid
    from app.drafts.translate import DraftTranslator
    app._draft_translator = DraftTranslator(app._provider, DraftGuard(db))
    app.jobs = None

    captured_events = []
    class FakeUi:
        def publish(self, event_type, data):
            captured_events.append((event_type, data))
    app.ui_server = FakeUi()

    await app._handle_draft(_make_job(seg_id))

    created = [e for e in captured_events if e[0] == "draft.created"]
    assert len(created) == 1
    payload = created[0][1]
    assert "gap_note" in payload
    assert "confidence" in payload
    assert "lang_ok" in payload
    assert "suggested_clarification" in payload


@pytest.mark.asyncio
async def test_draft_i2_returns_none(db):
    """I2 вернул None → нет записи."""
    sid, _, seg_id = await _seed_meeting_segment(db)
    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app._provider = FakeProvider()
    app._session_id = sid

    class ReturnNoneI2:
        async def generate(self, req, *, fence):
            return None
        def snapshot(self):
            return {}

    app._draft_provider = ReturnNoneI2()
    app._draft_guard = AcceptingGuard()
    app._library = FakeLibrary()
    app._draft_translator = None
    app.jobs = None

    await app._handle_draft(_make_job(seg_id))

    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM draft_answers WHERE trigger_segment_id = ?",
        (seg_id,),
    )
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_draft_stale_generation(db):
    """StaleGenerationError → тихо, нет записи."""
    sid, _, seg_id = await _seed_meeting_segment(db)
    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app._provider = FakeProvider()
    app._session_id = sid
    app._draft_provider = RaiseStaleI2()
    app._draft_guard = AcceptingGuard()
    app._library = FakeLibrary()
    app.jobs = None

    await app._handle_draft(_make_job(seg_id))

    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM draft_answers WHERE trigger_segment_id = ?",
        (seg_id,),
    )
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_draft_gate_blocked_re_enqueues(db):
    """Gate BLOCKED → задача отложена, провайдер не вызван."""
    from app.errors import ProviderAuthError
    from app.queue import JobQueue, QueueConfig

    sid, _, seg_id = await _seed_meeting_segment(db)
    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app._provider = FakeProvider()
    app._session_id = sid
    app._draft_provider = FakeDraftI2(succeed=True)
    from app.drafts.guardrails import DraftGuard
    app._draft_guard = DraftGuard(db)
    app._library = FakeLibrary()
    app.jobs = JobQueue(db, app.privacy, QueueConfig())
    app.jobs.register(JobType.DRAFT, app._handle_draft)
    await app.jobs.start()

    # Блокируем gate
    app.offline.mark_unavailable("fake", ProviderAuthError("bad key"))

    await app._handle_draft(_make_job(seg_id))

    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM draft_answers WHERE trigger_segment_id = ?",
        (seg_id,),
    )
    assert row["n"] == 0

    queued = await db.fetch_one(
        "SELECT id FROM jobs WHERE type = 'draft' AND segment_id = ? AND status = 'queued'",
        (seg_id,),
    )
    assert queued is not None

    await app.jobs.stop()


@pytest.mark.asyncio
async def test_draft_provider_error(db):
    """ProviderError → mark_unavailable + проброс."""
    sid, _, seg_id = await _seed_meeting_segment(db)
    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app._provider = FakeProvider()
    app._session_id = sid
    app._draft_provider = FakeDraftI2(succeed=False)
    app._draft_guard = AcceptingGuard()
    app._library = FakeLibrary()
    app.jobs = None

    with pytest.raises(ProviderUnavailable):
        await app._handle_draft(_make_job(seg_id))

    entry = app.offline._entry("fake")
    assert entry.last_error_code == "ProviderUnavailable"


@pytest.mark.asyncio
async def test_draft_microphone_role_returns_none(db):
    """microphone-сегмент → _load_draft_input вернёт None."""
    sid = uuid.uuid4().hex
    stream_id = uuid.uuid4().hex
    seg_id = uuid.uuid4().hex
    lib_id = uuid.uuid4().hex
    now = "2025-01-01T00:00:00.000"
    await db.execute(
        "INSERT INTO library_contexts (id, name, domain, content_text, token_estimate, updated_at) "
        "VALUES (?, ?, NULL, 'facts', 10, ?)",
        (lib_id, "test", now),
    )
    await db.execute(
        "INSERT INTO sessions (id, started_at, status, default_privacy_profile, mode, library_context_id) "
        "VALUES (?, ?, 'active', 'open', 'live_safe', ?)",
        (sid, now, lib_id),
    )
    await db.execute(
        "INSERT INTO audio_streams (id, session_id, role, source_language, target_language, enabled) "
        "VALUES (?, ?, 'microphone', 'ru', 'en', 1)",
        (stream_id, sid),
    )
    await db.execute(
        "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
        "privacy_profile, track, raw_text, translation_status, created_at) "
        "VALUES (?, ?, ?, 0, 1000, 'open', 'accurate', 'How much?', 'pending', ?)",
        (seg_id, sid, stream_id, now),
    )

    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app._session_id = sid
    app._provider = FakeProvider()
    app._draft_provider = FakeDraftI2(succeed=True)
    app._draft_guard = AcceptingGuard()
    app._library = FakeLibrary()
    app.jobs = None

    await app._handle_draft(_make_job(seg_id))

    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM draft_answers WHERE trigger_segment_id = ?",
        (seg_id,),
    )
    assert row["n"] == 0
