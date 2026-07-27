"""app/translation/context.py — D6 dynamic context window."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from app.db import Database


@dataclass(frozen=True, slots=True)
class ContextConfig:
    window_short: int = 4
    window_mid: int = 3
    window_long: int = 2
    short_chars: int = 100
    long_chars: int = 200


def window_for(length: int, cfg: ContextConfig) -> int:
    """Return the number of previous segments to fetch based on current segment length."""
    if length < cfg.short_chars:
        return cfg.window_short
    if length <= cfg.long_chars:
        return cfg.window_mid
    return cfg.window_long


async def build_context(db: Database, segment_id: str, cfg: ContextConfig) -> tuple[str, ...]:
    """
    Build context tuple for the given segment_id.
    Returns a tuple of raw_text strings in chronological order (oldest first).
    """
    # Fetch the current segment to get stream_id and t_start_ms
    current = await db.fetch_one(
        "SELECT stream_id, t_start_ms, raw_text FROM segments WHERE id = ?",
        (segment_id,),
    )
    if not current:
        from app.errors import InvariantViolation

        raise InvariantViolation("segment_not_found")

    stream_id = current["stream_id"]
    t_start_ms = current["t_start_ms"]
    # raw_text of current segment is not needed for context, but we could verify it's not empty
    # (the spec says we exclude the current segment anyway)

    # Determine how many previous segments we need
    # Length of current segment's raw_text (after trimming spaces)
    current_text = current["raw_text"] or ""
    length = len(current_text.strip())
    limit = window_for(length, cfg)

    if limit == 0:
        return ()

    # Fetch previous segments in the same stream, with track='accurate', and earlier start time
    rows = await db.fetch_all(
        """
        SELECT raw_text FROM segments
        WHERE stream_id = ?
          AND track = 'accurate'
          AND t_start_ms < ?
          AND raw_text IS NOT NULL
          AND TRIM(raw_text) <> ''
        ORDER BY t_start_ms DESC, id DESC
        LIMIT ?
        """,
        (stream_id, t_start_ms, limit),
    )
    # rows are in descending order (newest first). We need to reverse to chronological order.
    # Each row is a dict with key 'raw_text'
    texts = [row["raw_text"] for row in rows]
    # Reverse to get oldest first
    texts.reverse()
    return tuple(texts)