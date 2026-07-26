"""JSON export — full audit trail."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlite3 import Row
from typing import Any, Dict, List

from app.models import row_get


def to_json(session: Dict[str, Any], rows: List[Row], drafts: List[Row]) -> str:
    segments_out: list[dict[str, Any]] = []
    for row in rows:
        d: dict[str, Any] = {}
        for col in (
            "id", "t_start_ms", "t_end_ms", "role", "detected_language",
            "raw_text", "stt_confidence", "translation_raw", "translation_clean",
            "edit_log_json", "privacy_profile", "translation_status", "stt_model",
        ):
            val = row_get(row, col)
            d[col] = val
        edit_raw = row_get(row, "edit_log_json")
        if edit_raw is not None:
            try:
                d["edit_log_json"] = json.loads(edit_raw) if isinstance(edit_raw, str) else edit_raw
            except (json.JSONDecodeError, TypeError):
                d["edit_log_json"] = None
        else:
            d["edit_log_json"] = None
        segments_out.append(d)

    drafts_out: list[dict[str, Any]] = []
    for row in drafts:
        draft_item: dict[str, Any] = {}
        for col in (
            "id", "session_id", "trigger_segment_id", "draft_ru",
            "draft_translated", "target_language", "sources_json",
            "has_gaps", "gap_note", "status",
        ):
            val = row_get(row, col)
            draft_item[col] = val
        drafts_out.append(draft_item)

    payload: dict[str, Any] = {
        "session": session,
        "segments": segments_out,
        "drafts": drafts_out,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
