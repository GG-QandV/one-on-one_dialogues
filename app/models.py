"""app/models.py — типизированное представление строк БД и безопасное чтение. Задача B3 роадмапа."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from sqlite3 import Row
from typing import Any, Optional, Dict

# ------------------------------------------------------------------ enums


class Track(str, Enum):
    FAST = "fast"
    ACCURATE = "accurate"


class TranslationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class DraftStatus(str, Enum):
    GENERATED = "generated"
    IGNORED = "ignored"
    COPIED = "copied"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    FINISHED = "finished"
    ABORTED = "aborted"


class StreamRole(str, Enum):
    MEETING = "meeting"
    MICROPHONE = "microphone"


class StreamPriority(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class SessionMode(str, Enum):
    LIVE_LITERAL = "live_literal"
    LIVE_SAFE = "live_safe"
    POST_CLEAN = "post_clean"


class PrivacyProfile(str, Enum):
    OPEN = "open"
    CONFIDENTIAL = "confidential"


# ------------------------------------------------------------------ helpers


def _get_str(obj: Any, key: str, default: Any = None) -> Any:
    try:
        return obj[key]
    except (IndexError, KeyError):
        return default


def _get_int(obj: Any, key: str, default: Any = None) -> Optional[int]:
    val = _get_str(obj, key, default)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _get_float(obj: Any, key: str, default: Any = None) -> Optional[float]:
    val = _get_str(obj, key, default)
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _get_bool_int(obj: Any, key: str, default: Any = None) -> Optional[bool]:
    val = _get_str(obj, key, default)
    if val is None:
        return None
    try:
        return bool(int(val))
    except (ValueError, TypeError):
        return None


def _parse_json(json_str: Optional[str]) -> tuple[Any, bool]:
    if not json_str:
        return None, False
    try:
        return json.loads(json_str), False
    except (json.JSONDecodeError, TypeError):
        return None, True


def _enum_parse(enum_cls: type[Enum], value: Any) -> tuple[Any, Optional[Any]]:
    if value is None:
        return None, None
    try:
        return enum_cls(value), None
    except ValueError:
        return None, value


def row_get(row: Row, key: str, default: Any = None) -> Any:
    return _get_str(row, key, default)


# ------------------------------------------------------------------ row converters


def session_from_row(row: Row) -> "SessionRow":
    sid = _get_str(row, "id")
    started_at = _get_str(row, "started_at")
    ended_at = _get_str(row, "ended_at")
    meeting_title = _get_str(row, "meeting_title")
    status_raw = _get_str(row, "status")
    status, status_raw_invalid = _enum_parse(SessionStatus, status_raw)
    default_privacy_profile = _get_str(row, "default_privacy_profile")
    library_context_id = _get_str(row, "library_context_id")
    translation_provider = _get_str(row, "translation_provider")
    draft_provider = _get_str(row, "draft_provider")
    mode_raw = _get_str(row, "mode")
    mode, mode_raw_invalid = _enum_parse(SessionMode, mode_raw)
    return SessionRow(
        id=sid,
        started_at=started_at,
        ended_at=ended_at,
        meeting_title=meeting_title,
        status=status,
        status_raw=status_raw_invalid,
        default_privacy_profile=default_privacy_profile,
        library_context_id=library_context_id,
        translation_provider=translation_provider,
        draft_provider=draft_provider,
        mode=mode,
        mode_raw=mode_raw_invalid,
    )


def stream_from_row(row: Row) -> "AudioStreamRow":
    sid = _get_str(row, "id")
    session_id = _get_str(row, "session_id")
    role_raw = _get_str(row, "role")
    role, role_raw_invalid = _enum_parse(StreamRole, role_raw)
    source_language = _get_str(row, "source_language")
    target_language = _get_str(row, "target_language")
    pipewire_node = _get_str(row, "pipewire_node")
    enabled = _get_bool_int(row, "enabled")
    priority_raw = _get_str(row, "priority")
    priority, priority_raw_invalid = _enum_parse(StreamPriority, priority_raw)
    return AudioStreamRow(
        id=sid,
        session_id=session_id,
        role=role,
        role_raw=role_raw_invalid,
        source_language=source_language,
        target_language=target_language,
        pipewire_node=pipewire_node,
        enabled=enabled,
        priority=priority,
        priority_raw=priority_raw_invalid,
    )


def segment_from_row(row: Row) -> "SegmentRow":
    sid = _get_str(row, "id")
    session_id = _get_str(row, "session_id")
    stream_id = _get_str(row, "stream_id")
    t_start_ms = _get_int(row, "t_start_ms")
    t_end_ms = _get_int(row, "t_end_ms")
    local_audio_path = _get_str(row, "local_audio_path")
    privacy_profile = _get_str(row, "privacy_profile")
    track_raw = _get_str(row, "track")
    track, track_raw_invalid = _enum_parse(Track, track_raw)
    stt_model = _get_str(row, "stt_model")
    detected_language = _get_str(row, "detected_language")
    raw_text = _get_str(row, "raw_text")
    stt_confidence = _get_float(row, "stt_confidence")
    translation_status_raw = _get_str(row, "translation_status")
    ts, ts_raw_invalid = _enum_parse(TranslationStatus, translation_status_raw)
    translation_raw = _get_str(row, "translation_raw")
    translation_clean = _get_str(row, "translation_clean")
    edit_log_json_str = _get_str(row, "edit_log_json")
    edit_log_json, edit_log_malformed = _parse_json(edit_log_json_str)
    if edit_log_malformed or not isinstance(edit_log_json, dict):
        edit_log_json = None
    superseded_by_segment_id = _get_str(row, "superseded_by_segment_id")
    created_at = _get_str(row, "created_at")
    return SegmentRow(
        id=sid,
        session_id=session_id,
        stream_id=stream_id,
        t_start_ms=t_start_ms,
        t_end_ms=t_end_ms,
        local_audio_path=local_audio_path,
        privacy_profile=privacy_profile,
        track=track,
        track_raw=track_raw_invalid,
        stt_model=stt_model,
        detected_language=detected_language,
        raw_text=raw_text,
        stt_confidence=stt_confidence,
        translation_status=ts,
        translation_status_raw=ts_raw_invalid,
        translation_raw=translation_raw,
        translation_clean=translation_clean,
        edit_log_json=edit_log_json,
        edit_log_malformed=edit_log_malformed,
        superseded_by_segment_id=superseded_by_segment_id,
        created_at=created_at,
    )


def draft_from_row(row: Row) -> "DraftRow":
    did = _get_str(row, "id")
    session_id = _get_str(row, "session_id")
    trigger_segment_id = _get_str(row, "trigger_segment_id")
    draft_ru = _get_str(row, "draft_ru")
    draft_translated = _get_str(row, "draft_translated")
    target_language = _get_str(row, "target_language")
    sources_json_str = _get_str(row, "sources_json")
    sources_json, sources_malformed = _parse_json(sources_json_str)
    if sources_malformed or not isinstance(sources_json, list):
        sources_json = None
        sources = ()
    else:
        sources = tuple(sources_json)
    has_gaps = _get_bool_int(row, "has_gaps")
    gap_note = _get_str(row, "gap_note")
    status_raw = _get_str(row, "status")
    status, status_raw_invalid = _enum_parse(DraftStatus, status_raw)
    created_at = _get_str(row, "created_at")
    return DraftRow(
        id=did,
        session_id=session_id,
        trigger_segment_id=trigger_segment_id,
        draft_ru=draft_ru,
        draft_translated=draft_translated,
        target_language=target_language,
        sources_json=sources,
        sources_malformed=sources_malformed,
        has_gaps=has_gaps,
        gap_note=gap_note,
        status=status,
        status_raw=status_raw_invalid,
        created_at=created_at,
    )


def library_from_row(row: Row) -> "LibraryContextRow":
    lid = _get_str(row, "id")
    name = _get_str(row, "name")
    domain = _get_str(row, "domain")
    content_text = _get_str(row, "content_text")
    token_estimate = _get_int(row, "token_estimate")
    updated_at = _get_str(row, "updated_at")
    return LibraryContextRow(
        id=lid,
        name=name,
        domain=domain,
        content_text=content_text,
        token_estimate=token_estimate,
        updated_at=updated_at,
    )


# ------------------------------------------------------------------ dataclasses


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: Optional[str]
    started_at: Optional[str]
    ended_at: Optional[str]
    meeting_title: Optional[str]
    status: Optional[SessionStatus]
    status_raw: Optional[Any]
    default_privacy_profile: Optional[str]
    library_context_id: Optional[str]
    translation_provider: Optional[str]
    draft_provider: Optional[str]
    mode: Optional[SessionMode]
    mode_raw: Optional[Any]

    @property
    def is_active(self) -> bool:
        return self.status == SessionStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class AudioStreamRow:
    id: Optional[str]
    session_id: Optional[str]
    role: Optional[StreamRole]
    role_raw: Optional[Any]
    source_language: Optional[str]
    target_language: Optional[str]
    pipewire_node: Optional[str]
    enabled: Optional[bool]
    priority: Optional[StreamPriority]
    priority_raw: Optional[Any]


@dataclass(frozen=True, slots=True)
class SegmentRow:
    id: Optional[str]
    session_id: Optional[str]
    stream_id: Optional[str]
    t_start_ms: Optional[int]
    t_end_ms: Optional[int]
    local_audio_path: Optional[str]
    privacy_profile: Optional[str]
    track: Optional[Track]
    track_raw: Optional[Any]
    stt_model: Optional[str]
    detected_language: Optional[str]
    raw_text: Optional[str]
    stt_confidence: Optional[float]
    translation_status: Optional[TranslationStatus]
    translation_status_raw: Optional[Any]
    translation_raw: Optional[str]
    translation_clean: Optional[str]
    edit_log_json: Optional[Dict[str, Any]]
    edit_log_malformed: bool
    superseded_by_segment_id: Optional[str]
    created_at: Optional[str]

    @property
    def duration_ms(self) -> Optional[int]:
        if self.t_start_ms is None or self.t_end_ms is None:
            return None
        return self.t_end_ms - self.t_start_ms

    @property
    def is_translated(self) -> bool:
        return bool(self.translation_raw and self.translation_raw.strip())

    @property
    def is_superseded(self) -> bool:
        return bool(self.superseded_by_segment_id)


@dataclass(frozen=True, slots=True)
class DraftRow:
    id: Optional[str]
    session_id: Optional[str]
    trigger_segment_id: Optional[str]
    draft_ru: Optional[str]
    draft_translated: Optional[str]
    target_language: Optional[str]
    sources_json: tuple[str, ...]
    sources_malformed: bool
    has_gaps: Optional[bool]
    gap_note: Optional[str]
    status: Optional[DraftStatus]
    status_raw: Optional[Any]
    created_at: Optional[str]

    @property
    def was_used(self) -> bool:
        return self.status == DraftStatus.COPIED


@dataclass(frozen=True, slots=True)
class LibraryContextRow:
    id: Optional[str]
    name: Optional[str]
    domain: Optional[str]
    content_text: Optional[str]
    token_estimate: Optional[int]
    updated_at: Optional[str]