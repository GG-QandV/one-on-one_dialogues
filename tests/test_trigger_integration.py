"""Tests for I3 trigger integration in _on_stt_result."""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

from app.audio.segmenter import FinalSegment
from app.db import Database, DbConfig
from app.main import Application, AppConfig
from app.privacy import PrivacyController, PrivacyProfile
from app.queue import JobQueue, JobType, QueueConfig
from app.translation.offline import OfflineGate


class FakeSttResult:
    def __init__(self, text: str, model_used: str = "test"):
        self.payload = {"transcription": [{"text": text}]}
        self.model_used = model_used


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


async def _seed_job(db, seg_id: str, job_type: str):
    """Seed a segment + stream + session. Return (sid, stream_id)."""
    sid = uuid.uuid4().hex
    stream_id = uuid.uuid4().hex
    now = "2025-01-01T00:00:00.000"
    await db.execute(
        "INSERT INTO sessions (id, started_at, status, default_privacy_profile, mode) "
        "VALUES (?, ?, 'active', 'open', 'live_safe')",
        (sid, now),
    )
    await db.execute(
        "INSERT INTO audio_streams (id, session_id, role, source_language, target_language, enabled) "
        "VALUES (?, ?, ?, 'en', 'ru', 1)",
        (stream_id, sid, "meeting" if job_type == "meeting" else "microphone"),
    )
    await db.execute(
        "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
        "privacy_profile, track, raw_text, translation_status, created_at) "
        "VALUES (?, ?, ?, 0, 1000, 'open', 'accurate', 'How much?', 'pending', ?)",
        (seg_id, sid, stream_id, now),
    )
    return sid, stream_id


@pytest.mark.asyncio
async def test_trigger_integration_meeting_question_queues_draft(db):
    """meeting-сегмент с вопросом → задача DRAFT поставлена."""
    sid = uuid.uuid4().hex
    seg_id = uuid.uuid4().hex
    stream_id = uuid.uuid4().hex
    now = "2025-01-01T00:00:00.000"
    await db.execute(
        "INSERT INTO sessions (id, started_at, status, default_privacy_profile, mode) "
        "VALUES (?, ?, 'active', 'open', 'live_safe')",
        (sid, now),
    )
    await db.execute(
        "INSERT INTO audio_streams (id, session_id, role, source_language, target_language, enabled) "
        "VALUES (?, ?, 'meeting', 'en', 'ru', 1)",
        (stream_id, sid),
    )
    await db.execute(
        "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
        "privacy_profile, track, raw_text, translation_status, created_at) "
        "VALUES (?, ?, ?, 0, 1000, 'open', 'accurate', 'How much does it cost?', 'pending', ?)",
        (seg_id, sid, stream_id, now),
    )

    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app.jobs = JobQueue(db, app.privacy, QueueConfig())
    await app.jobs.start()
    from app.translation.supersede import SupersedeService
    app.supersede = SupersedeService(db)
    app._stream_languages["meeting"] = "en"

    seg = FinalSegment(
        id=seg_id, role="meeting", t_start_ms=0, t_end_ms=1000,
        audio_path=Path("/dev/null"), reason=None, mean_level_db=0.0,
    )
    raw = FakeSttResult("How much does it cost?")

    await app._on_stt_result(seg, raw)
    await asyncio.sleep(0.3)

    job = await db.fetch_one(
        "SELECT id FROM jobs WHERE type = 'draft' AND segment_id = ?",
        (seg_id,),
    )
    assert job is not None

    await app.jobs.stop()


@pytest.mark.asyncio
async def test_trigger_integration_microphone_no_draft(db):
    """microphone-сегмент → задача DRAFT НЕ поставлена."""
    sid = uuid.uuid4().hex
    seg_id = uuid.uuid4().hex
    stream_id = uuid.uuid4().hex
    now = "2025-01-01T00:00:00.000"
    await db.execute(
        "INSERT INTO sessions (id, started_at, status, default_privacy_profile, mode) "
        "VALUES (?, ?, 'active', 'open', 'live_safe')",
        (sid, now),
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
    app.jobs = JobQueue(db, app.privacy, QueueConfig())
    await app.jobs.start()
    from app.translation.supersede import SupersedeService
    app.supersede = SupersedeService(db)
    app._stream_languages["microphone"] = "ru"

    seg = FinalSegment(
        id=seg_id, role="microphone", t_start_ms=0, t_end_ms=1000,
        audio_path=Path("/dev/null"), reason=None, mean_level_db=0.0,
    )
    raw = FakeSttResult("How much?")
    def _tx(conn):
        conn.execute("UPDATE segments SET raw_text = ? WHERE id = ?",
                     ("How much?", seg.id))
    await db.write(_tx)
    await app.supersede.link(seg.id)
    await app._on_stt_result(seg, raw)
    await asyncio.sleep(0.3)

    queued = await db.fetch_one(
        "SELECT id FROM jobs WHERE type = 'draft' AND segment_id = ? AND status = 'queued'",
        (seg_id,),
    )
    assert queued is None

    await app.jobs.stop()




@pytest.mark.asyncio
async def test_trigger_integration_meeting_statement_no_draft(db):
    """meeting-сегмент-утверждение → задача DRAFT НЕ поставлена."""
    sid = uuid.uuid4().hex
    seg_id = uuid.uuid4().hex
    stream_id = uuid.uuid4().hex
    now = "2025-01-01T00:00:00.000"
    await db.execute(
        "INSERT INTO sessions (id, started_at, status, default_privacy_profile, mode) "
        "VALUES (?, ?, 'active', 'open', 'live_safe')",
        (sid, now),
    )
    await db.execute(
        "INSERT INTO audio_streams (id, session_id, role, source_language, target_language, enabled) "
        "VALUES (?, ?, 'meeting', 'en', 'ru', 1)",
        (stream_id, sid),
    )
    await db.execute(
        "INSERT INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, "
        "privacy_profile, track, raw_text, translation_status, created_at) "
        "VALUES (?, ?, ?, 0, 1000, 'open', 'accurate', 'We agreed on the terms.', 'pending', ?)",
        (seg_id, sid, stream_id, now),
    )

    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app.jobs = JobQueue(db, app.privacy, QueueConfig())
    app.jobs.register(JobType.DRAFT, lambda j: None)
    await app.jobs.start()
    from app.translation.supersede import SupersedeService
    app.supersede = SupersedeService(db)
    app._stream_languages["meeting"] = "en"

    seg = FinalSegment(
        id=seg_id, role="meeting", t_start_ms=0, t_end_ms=1000,
        audio_path=Path("/dev/null"), reason=None, mean_level_db=0.0,
    )
    raw = FakeSttResult("We agreed on the terms.")

    await app._on_stt_result(seg, raw)
    await asyncio.sleep(0.3)

    job = await db.fetch_one(
        "SELECT id FROM jobs WHERE type = 'draft' AND segment_id = ?",
        (seg_id,),
    )
    assert job is None

    await app.jobs.stop()


import asyncio
