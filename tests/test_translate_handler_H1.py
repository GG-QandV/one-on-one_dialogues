"""Tests for H1 TRANSLATE handler (app/main.py _handle_translate)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from app.db import Database, DbConfig
from app.errors import ProviderAuthError, ProviderUnavailable, StaleGenerationError
from app.main import Application, AppConfig
from app.privacy import Fence, PrivacyController, PrivacyProfile, Capability
from app.queue import Job, JobStatus, JobType
from app.translation.offline import ProviderState
from app.translation.base import TranslationRequest, TranslationResult, TranslationMode
from app.translation.offline import OfflineGate
from app.ui.server import EventType


class FakeProvider:
    name = "fake"

    def __init__(self, succeed: bool = True):
        self.privacy = PrivacyController(PrivacyProfile.OPEN)
        self._succeed = succeed
        self.last_request: TranslationRequest | None = None

    async def translate(self, req: TranslationRequest, *, fence):
        self.last_request = req
        if not self._succeed:
            raise ProviderUnavailable("fake unavailable")
        return TranslationResult(
            translation_raw="translated text",
            translation_clean="cleaned text",
        )

    async def close(self):
        pass


class RaiseStaleProvider:
    name = "stale"

    def __init__(self):
        self.privacy = PrivacyController(PrivacyProfile.OPEN)

    async def translate(self, req: TranslationRequest, *, fence):
        raise StaleGenerationError()

    async def close(self):
        pass


def _make_job(segment_id: str, profile=PrivacyProfile.OPEN, gen=0) -> Job:
    return Job(
        id=uuid.uuid4().hex,
        type=JobType.TRANSLATE,
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


# --------------------------------------------------------------- fixtures


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


@pytest.fixture
async def app(db):
    app_obj = Application(AppConfig())
    app_obj.db = db
    app_obj.privacy = PrivacyController(PrivacyProfile.OPEN)
    app_obj.offline = OfflineGate()
    app_obj._provider = FakeProvider(succeed=True)
    app_obj.jobs = None  # will set per test if needed
    yield app_obj


# --------------------------------------------------------------- helpers


async def _seed_segment(db: Database) -> tuple[str, str, str]:
    """Create session + stream + segment; return (session_id, stream_id, segment_id)."""
    sid = uuid.uuid4().hex
    stream_id = uuid.uuid4().hex
    seg_id = uuid.uuid4().hex
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
        "VALUES (?, ?, ?, 0, 1000, 'open', 'accurate', 'привет мир', 'pending', ?)",
        (seg_id, sid, stream_id, now),
    )
    return sid, stream_id, seg_id


# --------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_h1_positive_translate_writes_to_db(app, db):
    """Сырой текст → перевод записан, статус done, gate AVAILABLE."""
    _, _, seg_id = await _seed_segment(db)
    job = _make_job(seg_id)
    app.jobs = None  # not needed for positive path

    await app._handle_translate(job)

    row = await db.fetch_one(
        "SELECT translation_raw, translation_clean, translation_status FROM segments WHERE id = ?",
        (seg_id,),
    )
    assert row is not None
    assert row["translation_raw"] == "translated text"
    assert row["translation_clean"] == "cleaned text"
    assert row["translation_status"] == "done"
    assert app.offline.should_attempt("fake") is True


@pytest.mark.asyncio
async def test_h1_provider_unavailable_marks_gate_and_does_not_write(app, db):
    """ProviderUnavailable → gate BLOCKED, статус НЕ done, исключение пробрасывается."""
    _, _, seg_id = await _seed_segment(db)
    app._provider = FakeProvider(succeed=False)
    job = _make_job(seg_id)

    with pytest.raises(ProviderUnavailable):
        await app._handle_translate(job)

    # Перевод не записан
    row = await db.fetch_one(
        "SELECT translation_raw, translation_status FROM segments WHERE id = ?",
        (seg_id,),
    )
    assert row["translation_raw"] is None
    assert row["translation_status"] != "done"
    # Gate учёл отказ (consecutive_failures >= 1, но DEGRADED только после 3)
    entry = app.offline._entry("fake")
    assert entry.consecutive_failures >= 1
    assert entry.last_error_code == "ProviderUnavailable"


@pytest.mark.asyncio
async def test_h1_stale_generation_silent_no_write(app, db):
    """StaleGenerationError → перевод НЕ записан, задача завершена тихо."""
    _, _, seg_id = await _seed_segment(db)
    app._provider = RaiseStaleProvider()
    job = _make_job(seg_id, gen=0)

    # Должно пройти без исключения
    await app._handle_translate(job)

    row = await db.fetch_one(
        "SELECT translation_raw, translation_status FROM segments WHERE id = ?",
        (seg_id,),
    )
    assert row["translation_raw"] is None
    assert row["translation_status"] != "done"


@pytest.mark.asyncio
async def test_h1_gate_blocked_re_enqueues(app, db):
    """Gate BLOCKED → провайдер не вызывается, задача отложена."""
    from app.queue import JobQueue, QueueConfig

    app.jobs = JobQueue(db, app.privacy, QueueConfig())
    app.jobs.register(JobType.TRANSLATE, app._handle_translate)
    await app.jobs.start()

    _, _, seg_id = await _seed_segment(db)
    app._provider = FakeProvider(succeed=True)
    # Принудительно блокируем gate (ProviderAuthError → immediate BLOCKED)
    app.offline.mark_unavailable("fake", ProviderAuthError("bad key"))
    assert app.offline.should_attempt("fake") is False

    job = _make_job(seg_id)
    await app._handle_translate(job)

    # Перевод не записан
    row = await db.fetch_one(
        "SELECT translation_raw, translation_status FROM segments WHERE id = ?",
        (seg_id,),
    )
    assert row["translation_raw"] is None
    assert row["translation_status"] != "done"

    # Задача отложена — проверим что в очереди есть QUEUED TRANSLATE
    queued = await db.fetch_one(
        "SELECT id FROM jobs WHERE type = ? AND segment_id = ? AND status = 'queued'",
        ("translate", seg_id),
    )
    assert queued is not None

    await app.jobs.stop()
