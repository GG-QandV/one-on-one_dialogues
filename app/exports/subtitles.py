"""SRT and VTT subtitle export."""

from __future__ import annotations

from sqlite3 import Row
from typing import List

from app.models import row_get


def format_timestamp_srt(ms: int) -> str:
    ms = max(0, ms)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_timestamp_vtt(ms: int) -> str:
    return format_timestamp_srt(ms).replace(",", ".")


def _sanitize(text: str) -> str:
    result = []
    for ch in text:
        cp = ord(ch)
        if cp < 0x09:
            continue
        if 0x0B <= cp <= 0x0C:
            continue
        if 0x0E <= cp <= 0x1F:
            continue
        result.append(ch)
    return "".join(result)


def _text(row: Row) -> str:
    t = row_get(row, "translation_raw") or row_get(row, "translation_clean") or row_get(row, "raw_text") or ""
    return _sanitize(str(t))


def _safe_range(row: Row) -> tuple[int, int]:
    start = row_get(row, "t_start_ms") or 0
    end = row_get(row, "t_end_ms") or start
    if end <= start:
        end = start + 1000
    return start, end


def to_srt(rows: List[Row]) -> str:
    if not rows:
        return ""
    blocks: list[str] = []
    for i, row in enumerate(rows, 1):
        start, end = _safe_range(row)
        text = _text(row)
        block = (
            f"{i}\r\n"
            f"{format_timestamp_srt(start)} --> {format_timestamp_srt(end)}\r\n"
            f"{text}"
        )
        blocks.append(block)
    return "\r\n\r\n".join(blocks) + "\r\n"


def to_vtt(rows: List[Row]) -> str:
    if not rows:
        return "WEBVTT\n\n"
    lines = ["WEBVTT", ""]
    for row in rows:
        start, end = _safe_range(row)
        text = _text(row)
        lines.append(f"{format_timestamp_vtt(start)} --> {format_timestamp_vtt(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)
