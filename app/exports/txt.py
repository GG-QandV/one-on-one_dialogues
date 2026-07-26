"""TXT export — plain text transcript."""

from __future__ import annotations

from sqlite3 import Row
from typing import List

from app.models import row_get


def _fmt_ts(ms: int) -> str:
    ms = max(0, ms)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s = ms // 1000
    return f"{h:02d}:{m:02d}:{s:02d}"


def to_txt(rows: List[Row]) -> str:
    blocks: list[str] = []
    for row in rows:
        ts = _fmt_ts(row_get(row, "t_start_ms", 0))
        role = row_get(row, "role") or row_get(row, "track", "?")
        lang = row_get(row, "detected_language") or "??"
        raw = row_get(row, "raw_text")
        translation = row_get(row, "translation_raw") or row_get(row, "translation_clean")
        block = f"[{ts}] {role} · {lang}"
        if raw:
            block += f"\n{raw}"
        if translation:
            block += f"\n{translation}"
        blocks.append(block)
    return "\n\n".join(blocks)
