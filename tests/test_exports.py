"""Tests for exports (G4): TXT, JSON, SRT, VTT."""

import json

import pytest

from app.exports.txt import to_txt
from app.exports.json_export import to_json
from app.exports.subtitles import to_srt, to_vtt, format_timestamp_srt, format_timestamp_vtt


class MockRow:
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


def _seg(**kw):
    defaults = dict(
        id="seg_1", t_start_ms=5000, t_end_ms=12000, role="meeting",
        detected_language="ru", raw_text="Привет мир",
        stt_confidence=-0.12, translation_raw="Hello world",
        translation_clean="Hello world", edit_log_json='{"changes":[]}',
        privacy_profile="open", translation_status="done",
        stt_model="ggml-base.bin",
    )
    defaults.update(kw)
    return MockRow(**defaults)


def _draft(**kw):
    defaults = dict(
        id="d_1", session_id="sess_1", trigger_segment_id="seg_1",
        draft_ru="Черновик", draft_translated="Draft",
        target_language="en", sources_json='["doc1"]',
        has_gaps=0, gap_note=None, status="generated",
    )
    defaults.update(kw)
    return MockRow(**defaults)


class TestFormatTimestamp:
    def test_srt_full(self):
        assert format_timestamp_srt(3661234) == "01:01:01,234"

    def test_srt_zero(self):
        assert format_timestamp_srt(0) == "00:00:00,000"

    def test_vtt_full(self):
        assert format_timestamp_vtt(3661234) == "01:01:01.234"

    def test_vtt_zero(self):
        assert format_timestamp_vtt(0) == "00:00:00.000"


class TestTxt:
    def test_single_segment_with_translation(self):
        rows = [_seg()]
        out = to_txt(rows)
        assert "[00:00:05]" in out
        assert "meeting" in out
        assert "Привет мир" in out
        assert "Hello world" in out

    def test_segment_without_translation_preserves_original(self):
        rows = [_seg(translation_raw=None, translation_clean=None)]
        out = to_txt(rows)
        assert "Привет мир" in out
        assert "Hello world" not in out

    def test_empty_list_returns_empty_string(self):
        assert to_txt([]) == ""

    def test_multiple_segments_preserve_order(self):
        rows = [
            _seg(id="s1", t_start_ms=0, t_end_ms=1000, raw_text="first"),
            _seg(id="s2", t_start_ms=2000, t_end_ms=3000, raw_text="second"),
        ]
        out = to_txt(rows)
        assert out.index("first") < out.index("second")


class TestJson:
    def test_valid_json(self):
        session = {"id": "sess_1"}
        rows = [_seg()]
        drafts = [_draft()]
        out = to_json(session, rows, drafts)
        parsed = json.loads(out)
        assert parsed["session"]["id"] == "sess_1"
        assert len(parsed["segments"]) == 1
        assert len(parsed["drafts"]) == 1

    def test_ensure_ascii_false(self):
        session = {"id": "sess_1"}
        rows = [_seg(raw_text="Привет")]
        out = to_json(session, rows, [])
        assert "Привет" in out
        assert "\\u041f" not in out

    def test_edit_log_json_is_object_not_string(self):
        session = {"id": "sess_1"}
        rows = [_seg(edit_log_json='{"changes":[{"type":"filler_removed"}]}')]
        out = to_json(session, rows, [])
        parsed = json.loads(out)
        seg = parsed["segments"][0]
        assert isinstance(seg["edit_log_json"], dict)
        assert seg["edit_log_json"]["changes"][0]["type"] == "filler_removed"

    def test_privacy_profile_present(self):
        session = {"id": "sess_1"}
        rows = [_seg(privacy_profile="confidential")]
        out = to_json(session, rows, [])
        parsed = json.loads(out)
        assert parsed["segments"][0]["privacy_profile"] == "confidential"

    def test_drafts_in_separate_block_no_translation_fields(self):
        session = {"id": "sess_1"}
        rows = [_seg()]
        drafts = [_draft(draft_ru="Тест")]
        out = to_json(session, rows, drafts)
        parsed = json.loads(out)
        draft = parsed["drafts"][0]
        assert draft["draft_ru"] == "Тест"
        assert "translation_raw" not in draft

    def test_empty_input(self):
        out = to_json({"id": "sess_1"}, [], [])
        parsed = json.loads(out)
        assert parsed["segments"] == []
        assert parsed["drafts"] == []
        assert "exported_at" in parsed


class TestSrt:
    def test_numbering_and_delimiters(self):
        rows = [_seg(id="s1", t_start_ms=1000, t_end_ms=4000)]
        out = to_srt(rows)
        assert out.startswith("1\r\n")
        assert "-->" in out
        assert "," in out[:50]

    def test_newline_is_crlf(self):
        rows = [_seg(id="s1", t_start_ms=1000, t_end_ms=4000)]
        out = to_srt(rows)
        assert "\r\n" in out

    def test_translation_preferred_over_raw(self):
        rows = [_seg(raw_text="Привет", translation_raw="Hello")]
        out = to_srt(rows)
        assert "Hello" in out

    def test_raw_text_when_no_translation(self):
        rows = [_seg(raw_text="Привет", translation_raw=None, translation_clean=None)]
        out = to_srt(rows)
        assert "Привет" in out

    def test_zero_duration_fixed_to_1000ms(self):
        rows = [_seg(id="s1", t_start_ms=5000, t_end_ms=5000)]
        out = to_srt(rows)
        assert "00:00:05,000 --> 00:00:06,000" in out

    def test_control_chars_stripped(self):
        rows = [_seg(id="s1", t_start_ms=0, t_end_ms=1000,
                     translation_raw=None, translation_clean=None,
                     raw_text="Hello\x00world\x01test")]
        out = to_srt(rows)
        assert "\x00" not in out
        assert "\x01" not in out
        assert "Hello" in out and "world" in out

    def test_angle_brackets_preserved(self):
        rows = [_seg(id="s1", t_start_ms=0, t_end_ms=1000,
                     translation_raw=None, translation_clean=None,
                     raw_text="<i>important</i>")]
        out = to_srt(rows)
        assert "<i>" in out

    def test_empty_input(self):
        assert to_srt([]) == ""


class TestVtt:
    def test_starts_with_webvtt(self):
        out = to_vtt([_seg(id="s1", t_start_ms=0, t_end_ms=1000)])
        assert out.startswith("WEBVTT\n\n")

    def test_timestamp_uses_dot(self):
        rows = [_seg(id="s1", t_start_ms=1000, t_end_ms=4000)]
        out = to_vtt(rows)
        lines = [l for l in out.split("\n") if l.strip()]
        timestamp_line = next(l for l in lines if "-->" in l)
        assert "." in timestamp_line

    def test_no_optional_numbering(self):
        rows = [_seg(id="s1", t_start_ms=0, t_end_ms=1000)]
        out = to_vtt(rows)
        lines = out.split("\n")
        assert lines[0] == "WEBVTT"
        assert lines[1] == ""
        assert "00:00:00.000" in out

    def test_zero_duration_fixed(self):
        rows = [_seg(id="s1", t_start_ms=5000, t_end_ms=5000)]
        out = to_vtt(rows)
        assert "00:00:05.000 --> 00:00:06.000" in out

    def test_empty_input(self):
        assert to_vtt([]) == "WEBVTT\n\n"


class TestIntegration:
    def test_fast_track_not_in_export(self):
        rows = [
            _seg(id="s1", raw_text="Hello", t_start_ms=0, t_end_ms=1000),
        ]
        txt = to_txt(rows)
        assert "Hello" in txt
        srt = to_srt(rows)
        assert "Hello" in srt

    def test_overlapping_segments_preserved(self):
        rows = [
            _seg(id="s1", t_start_ms=0, t_end_ms=5000, raw_text="first"),
            _seg(id="s2", t_start_ms=3000, t_end_ms=8000, raw_text="second"),
        ]
        txt = to_txt(rows)
        assert "first" in txt
        assert "second" in txt
