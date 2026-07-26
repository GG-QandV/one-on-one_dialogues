"""Tests for models.py (B3)."""

import json
from dataclasses import FrozenInstanceError

import pytest

from app.models import (
    AudioStreamRow,
    DraftRow,
    DraftStatus,
    LibraryContextRow,
    SegmentRow,
    SessionMode,
    SessionRow,
    SessionStatus,
    StreamPriority,
    StreamRole,
    Track,
    TranslationStatus,
    draft_from_row,
    library_from_row,
    row_get,
    segment_from_row,
    session_from_row,
    stream_from_row,
)


class MockRow:
    """Mimics sqlite3.Row: supports both string and int indexing, raises IndexError on missing."""

    def __init__(self, **kwargs):
        self._keys = list(kwargs.keys())
        self._vals = list(kwargs.values())
        self._map = kwargs

    def __getitem__(self, key):
        if isinstance(key, str):
            if key in self._map:
                return self._map[key]
            raise IndexError(key)
        if isinstance(key, int):
            return self._vals[key]
        raise IndexError(key)

    def keys(self):
        return self._keys


@pytest.fixture
def full_segment_row():
    return MockRow(
        id="seg_1",
        session_id="sess_1",
        stream_id="str_1",
        t_start_ms=1000,
        t_end_ms=5000,
        local_audio_path="/tmp/audio.wav",
        privacy_profile="open",
        track="accurate",
        stt_model="ggml-base.bin",
        detected_language="ru",
        raw_text="Привет мир",
        stt_confidence=-0.12,
        translation_status="done",
        translation_raw="Hello world",
        translation_clean="Hello world",
        edit_log_json='{"changes":[]}',
        superseded_by_segment_id=None,
        created_at="2026-07-26T10:00:00Z",
    )


@pytest.fixture
def full_session_row():
    return MockRow(
        id="sess_1",
        started_at="2026-07-26T10:00:00Z",
        ended_at=None,
        meeting_title="Test Meeting",
        status="active",
        default_privacy_profile="open",
        library_context_id=None,
        translation_provider="gemini",
        draft_provider="gemini",
        mode="live_literal",
    )


@pytest.fixture
def full_stream_row():
    return MockRow(
        id="str_1",
        session_id="sess_1",
        role="meeting",
        source_language="en",
        target_language="ru",
        pipewire_node="alsa_input.usb-...",
        enabled=1,
        priority="primary",
    )


@pytest.fixture
def full_draft_row():
    return MockRow(
        id="draft_1",
        session_id="sess_1",
        trigger_segment_id="seg_1",
        draft_ru="Черновик ответа",
        draft_translated="Draft reply",
        target_language="en",
        sources_json='["doc1", "doc2"]',
        has_gaps=0,
        gap_note=None,
        status="generated",
        created_at="2026-07-26T10:00:00Z",
    )


@pytest.fixture
def full_library_row():
    return MockRow(
        id="lib_1",
        name="Contract Terms",
        domain="legal",
        content_text="Some content here",
        token_estimate=1500,
        updated_at="2026-07-26T10:00:00Z",
    )


class TestEnums:
    def test_track_values(self):
        assert Track.FAST == "fast"
        assert Track.ACCURATE == "accurate"

    def test_session_status_includes_paused(self):
        assert SessionStatus.PAUSED == "paused"

    def test_enum_ddl_match(self):
        assert Track.FAST.value == "fast"
        assert Track.ACCURATE.value == "accurate"
        assert SessionStatus.ACTIVE.value == "active"
        assert SessionStatus.PAUSED.value == "paused"
        assert SessionStatus.FINISHED.value == "finished"
        assert SessionStatus.ABORTED.value == "aborted"
        assert TranslationStatus.PENDING.value == "pending"
        assert TranslationStatus.RUNNING.value == "running"
        assert TranslationStatus.DONE.value == "done"
        assert TranslationStatus.FAILED.value == "failed"
        assert TranslationStatus.SKIPPED.value == "skipped"
        assert DraftStatus.GENERATED.value == "generated"
        assert DraftStatus.IGNORED.value == "ignored"
        assert DraftStatus.COPIED.value == "copied"
        assert StreamRole.MEETING.value == "meeting"
        assert StreamRole.MICROPHONE.value == "microphone"
        assert StreamPriority.PRIMARY.value == "primary"
        assert StreamPriority.SECONDARY.value == "secondary"


class TestRowGet:
    def test_missing_column_returns_default(self):
        row = MockRow(id="1", name="test")
        assert row_get(row, "id") == "1"
        assert row_get(row, "nonexistent") is None
        assert row_get(row, "nonexistent", "fallback") == "fallback"

    def test_index_error_on_missing(self):
        row = MockRow(id="1")
        assert row_get(row, "id") == "1"
        assert row_get(row, "missing") is None


class TestSegmentFromRow:
    def test_full_row_parses_all_fields(self, full_segment_row):
        seg = segment_from_row(full_segment_row)
        assert seg.id == "seg_1"
        assert seg.session_id == "sess_1"
        assert seg.stream_id == "str_1"
        assert seg.t_start_ms == 1000
        assert seg.t_end_ms == 5000
        assert seg.local_audio_path == "/tmp/audio.wav"
        assert seg.privacy_profile == "open"
        assert seg.track == Track.ACCURATE
        assert seg.track_raw is None
        assert seg.stt_model == "ggml-base.bin"
        assert seg.detected_language == "ru"
        assert seg.raw_text == "Привет мир"
        assert seg.stt_confidence == -0.12
        assert seg.translation_status == TranslationStatus.DONE
        assert seg.translation_status_raw is None
        assert seg.translation_raw == "Hello world"
        assert seg.translation_clean == "Hello world"
        assert seg.edit_log_json == {"changes": []}
        assert seg.edit_log_malformed is False
        assert seg.superseded_by_segment_id is None
        assert seg.created_at == "2026-07-26T10:00:00Z"

    def test_partial_select_returns_none_for_unread(self):
        row = MockRow(id="seg_1", raw_text="test")
        seg = segment_from_row(row)
        assert seg.id == "seg_1"
        assert seg.raw_text == "test"
        assert seg.session_id is None
        assert seg.t_start_ms is None
        assert seg.t_end_ms is None
        assert seg.track is None

    def test_unknown_track_parses_softly(self):
        row = MockRow(track="unknown_val", id="seg_1")
        seg = segment_from_row(row)
        assert seg.track is None
        assert seg.track_raw == "unknown_val"

    def test_edit_log_json_valid(self, full_segment_row):
        seg = segment_from_row(full_segment_row)
        assert seg.edit_log_json == {"changes": []}
        assert seg.edit_log_malformed is False

    def test_edit_log_json_broken(self):
        row = MockRow(edit_log_json="{invalid json}}", id="seg_1")
        seg = segment_from_row(row)
        assert seg.edit_log_json is None
        assert seg.edit_log_malformed is True

    def test_duration_ms(self, full_segment_row):
        seg = segment_from_row(full_segment_row)
        assert seg.duration_ms == 4000

    def test_duration_ms_none_when_t_end_missing(self):
        row = MockRow(id="seg_1", t_start_ms=1000, t_end_ms=None)
        seg = segment_from_row(row)
        assert seg.duration_ms is None

    def test_is_superseded_true(self):
        row = MockRow(id="seg_1", superseded_by_segment_id="seg_0")
        seg = segment_from_row(row)
        assert seg.is_superseded is True

    def test_is_superseded_false(self, full_segment_row):
        seg = segment_from_row(full_segment_row)
        assert seg.is_superseded is False

    def test_is_translated(self, full_segment_row):
        seg = segment_from_row(full_segment_row)
        assert seg.is_translated is True

    def test_is_translated_false_when_empty(self):
        row = MockRow(id="seg_1", translation_raw=None)
        seg = segment_from_row(row)
        assert seg.is_translated is False


class TestSessionFromRow:
    def test_full_session(self, full_session_row):
        sess = session_from_row(full_session_row)
        assert sess.id == "sess_1"
        assert sess.started_at == "2026-07-26T10:00:00Z"
        assert sess.ended_at is None
        assert sess.meeting_title == "Test Meeting"
        assert sess.status == SessionStatus.ACTIVE
        assert sess.status_raw is None
        assert sess.default_privacy_profile == "open"
        assert sess.translation_provider == "gemini"
        assert sess.mode == SessionMode.LIVE_LITERAL
        assert sess.is_active is True

    def test_is_active_false(self):
        row = MockRow(id="sess_1", status="finished")
        sess = session_from_row(row)
        assert sess.is_active is False

    def test_partial_session(self):
        row = MockRow(id="sess_1")
        sess = session_from_row(row)
        assert sess.id == "sess_1"
        assert sess.started_at is None
        assert sess.status is None


class TestStreamFromRow:
    def test_full_stream(self, full_stream_row):
        s = stream_from_row(full_stream_row)
        assert s.id == "str_1"
        assert s.session_id == "sess_1"
        assert s.role == StreamRole.MEETING
        assert s.role_raw is None
        assert s.source_language == "en"
        assert s.target_language == "ru"
        assert s.enabled is True
        assert s.priority == StreamPriority.PRIMARY
        assert s.priority_raw is None

    def test_enabled_as_zero(self):
        row = MockRow(id="str_1", enabled=0, role="meeting", priority="secondary")
        s = stream_from_row(row)
        assert s.enabled is False


class TestDraftFromRow:
    def test_full_draft(self, full_draft_row):
        d = draft_from_row(full_draft_row)
        assert d.id == "draft_1"
        assert d.session_id == "sess_1"
        assert d.trigger_segment_id == "seg_1"
        assert d.draft_ru == "Черновик ответа"
        assert d.target_language == "en"
        assert d.sources_json == ("doc1", "doc2")
        assert d.sources_malformed is False
        assert d.has_gaps is False
        assert d.status == DraftStatus.GENERATED
        assert d.was_used is False

    def test_was_used_when_copied(self):
        row = MockRow(id="draft_1", status="copied", sources_json="[]")
        d = draft_from_row(row)
        assert d.was_used is True

    def test_sources_json_broken(self):
        row = MockRow(id="draft_1", sources_json="not json", status="generated")
        d = draft_from_row(row)
        assert d.sources_json == ()
        assert d.sources_malformed is True


class TestLibraryFromRow:
    def test_full_library(self, full_library_row):
        lib = library_from_row(full_library_row)
        assert lib.id == "lib_1"
        assert lib.name == "Contract Terms"
        assert lib.domain == "legal"
        assert lib.content_text == "Some content here"
        assert lib.token_estimate == 1500
        assert lib.updated_at == "2026-07-26T10:00:00Z"


class TestFrozen:
    def test_cannot_modify_session_row(self, full_session_row):
        sess = session_from_row(full_session_row)
        with pytest.raises(FrozenInstanceError):
            sess.id = "changed"

    def test_cannot_modify_segment_row(self, full_segment_row):
        seg = segment_from_row(full_segment_row)
        with pytest.raises(FrozenInstanceError):
            seg.raw_text = "modified"

    def test_cannot_modify_draft_row(self, full_draft_row):
        d = draft_from_row(full_draft_row)
        with pytest.raises(FrozenInstanceError):
            d.draft_ru = "modified"
