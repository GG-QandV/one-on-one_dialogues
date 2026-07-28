"""Tests for I4 draft translation wiring in _handle_draft."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from app.db import Database, DbConfig
from app.drafts.guardrails import DraftCandidate, DraftGuard, GuardConfig
from app.drafts.provider import DraftRequest
from app.main import Application, AppConfig
from app.privacy import PrivacyController, PrivacyProfile
from app.queue import Job, JobStatus, JobType
from app.translation.base import TranslationResult, TranslationRequest
from app.translation.offline import OfflineGate


class FakeProviderForI4:
    name = "fake"

    def __init__(self, succeed: bool = True, result_text: str = "translated text"):
        self.succeed = succeed
        self.result_text = result_text
        self.last_request: TranslationRequest | None = None
        self.privacy = PrivacyController(PrivacyProfile.OPEN)

    async def translate(self, req: TranslationRequest, *, fence):
        self.last_request = req
        if not self.succeed:
            from app.errors import ProviderUnavailable
            raise ProviderUnavailable("fake unavailable")
        return TranslationResult(
            translation_raw=self.result_text,
            translation_clean=self.result_text,
            changes=(),
        )

    async def close(self):
        pass


class FakeDraftI2ForI4:
    def __init__(self, candidate: DraftCandidate | None = None):
        self._candidate = candidate

    async def generate(self, req, *, fence):
        if self._candidate:
            return self._candidate
        return DraftCandidate(
            session_id=req.session_id,
            trigger_segment_id=req.trigger_segment_id,
            draft_ru="Ответ на русском",
            target_language=req.target_language,
            sources=("fact1",),
            has_gaps_claimed=False,
            gap_note=None,
        )

    def snapshot(self):
        return {}


class FakeDraftGuardForI4:
    def __init__(self):
        self.last_candidate = None
        self.attached: list[tuple[str, str]] = []

    def verify(self, candidate, library_text):
        from app.drafts.guardrails import Verdict, VerdictKind
        return Verdict(VerdictKind.ACCEPT, (), ())

    async def store(self, candidate, verdict):
        return uuid.uuid4().hex

    async def attach_translation(self, draft_id, translated):
        self.attached.append((draft_id, translated))

    async def mark(self, draft_id, status):
        pass

    async def stats(self, session_id):
        return {}


class FakeLibraryForI4:
    async def get(self, context_id):
        from app.drafts.library import LibraryContext
        return LibraryContext(
            id=context_id, name="test", domain=None,
            content_text="факт 100",
            token_estimate=10, updated_at="2025-01-01T00:00:00",
        )


def _make_job(segment_id, profile=PrivacyProfile.OPEN, gen=0):
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


async def _seed_for_draft(db, *, tgt_lang="en"):
    """Seed session + stream + segment + library. Return (sid, seg_id)."""
    sid = uuid.uuid4().hex
    stream_id = uuid.uuid4().hex
    seg_id = uuid.uuid4().hex
    lib_id = uuid.uuid4().hex
    now = "2025-01-01T00:00:00.000"
    await db.execute(
        "INSERT INTO library_contexts (id, name, domain, content_text, token_estimate, updated_at) "
        "VALUES (?, ?, NULL, 'факт 100', 10, ?)",
        (lib_id, "test", now),
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
    return sid, seg_id


# --------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_i4_success_attaches_translation(db):
    """Успешный перевод → attach_translation вызван, событие."""
    sid, seg_id = await _seed_for_draft(db, tgt_lang="fr")
    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app._provider = FakeProviderForI4(succeed=True)
    app._draft_provider = FakeDraftI2ForI4()
    guard = FakeDraftGuardForI4()
    app._draft_guard = guard
    app._library = FakeLibraryForI4()
    app._session_id = sid
    app.jobs = None

    from app.drafts.translate import DraftTranslator
    app._draft_translator = DraftTranslator(app._provider, guard)

    await app._handle_draft(_make_job(seg_id))

    assert len(guard.attached) == 1
    draft_id, translated = guard.attached[0]
    assert translated == "translated text"


@pytest.mark.asyncio
async def test_i4_drift_returns_none_no_attach(db):
    """Дрейф (lost_entity) → translate_draft вернул None → attach не вызван."""
    sid, seg_id = await _seed_for_draft(db, tgt_lang="fr")

    from app.drafts.guardrails import Verdict, VerdictKind
    from app.translation.base import Change

    class DriftProvider:
        name = "drift"
        privacy = PrivacyController(PrivacyProfile.OPEN)

        async def translate(self, req, *, fence):
            return TranslationResult(
                translation_raw="text",
                translation_clean="text",
                changes=(Change(type="lost_entity", original="100", replacement=""),),
            )

        async def close(self):
            pass

    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app._provider = DriftProvider()
    app._draft_provider = FakeDraftI2ForI4()
    guard = FakeDraftGuardForI4()
    app._draft_guard = guard
    app._library = FakeLibraryForI4()
    app._session_id = sid
    app.jobs = None

    from app.drafts.translate import DraftTranslator, DraftTranslateConfig
    app._draft_translator = DraftTranslator(app._provider, guard)

    await app._handle_draft(_make_job(seg_id))

    assert len(guard.attached) == 0


@pytest.mark.asyncio
async def test_i4_same_language_skips_translate(db):
    """Совпадение языков → провайдер не вызван, attach не вызван."""
    sid, seg_id = await _seed_for_draft(db, tgt_lang="en")  # target=en == generate_language=en default → skip

    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    provider = FakeProviderForI4(succeed=True)
    app._provider = provider
    app._draft_provider = FakeDraftI2ForI4()
    guard = FakeDraftGuardForI4()
    app._draft_guard = guard
    app._library = FakeLibraryForI4()
    app._session_id = sid
    app.jobs = None

    from app.drafts.translate import DraftTranslator, DraftTranslateConfig
    app._draft_translator = DraftTranslator(app._provider, guard)

    await app._handle_draft(_make_job(seg_id))

    assert provider.last_request is None  # провайдер не вызван
    assert len(guard.attached) == 0


@pytest.mark.asyncio
async def test_i4_source_language_param_passed(db):
    """source_language параметр → req.source_language == переданному."""
    sid, seg_id = await _seed_for_draft(db, tgt_lang="fr")

    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    provider = FakeProviderForI4(succeed=True)
    app._provider = provider
    app._draft_provider = FakeDraftI2ForI4()
    guard = FakeDraftGuardForI4()
    app._draft_guard = guard
    app._library = FakeLibraryForI4()
    app._session_id = sid
    app.jobs = None

    from app.drafts.translate import DraftTranslator
    app._draft_translator = DraftTranslator(app._provider, guard)

    await app._handle_draft(_make_job(seg_id))

    assert provider.last_request is not None
    assert provider.last_request.source_language == "en"


@pytest.mark.asyncio
async def test_i4_provider_error_does_not_fail_job(db):
    """ProviderError при переводе → задача не упала, черновик сохранён."""
    sid, seg_id = await _seed_for_draft(db, tgt_lang="fr")

    app = Application(AppConfig())
    app.db = db
    app.privacy = PrivacyController(PrivacyProfile.OPEN)
    app.offline = OfflineGate()
    app._provider = FakeProviderForI4(succeed=False)
    app._draft_provider = FakeDraftI2ForI4()
    guard = FakeDraftGuardForI4()
    app._draft_guard = guard
    app._library = FakeLibraryForI4()
    app._session_id = sid
    app.jobs = None

    from app.drafts.translate import DraftTranslator
    app._draft_translator = DraftTranslator(app._provider, guard)

    await app._handle_draft(_make_job(seg_id))

    assert len(guard.attached) == 0
