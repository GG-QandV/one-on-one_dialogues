from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

from app.db import Database, DbConfig
from app.translation.supersede import SupersedeService, SupersedeConfig


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


async def _seed_session_stream(db):
    sid = uuid.uuid4().hex
    stream_id = uuid.uuid4().hex
    now = "2025-01-01T00:00:00.000"

    def _tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, started_at, status, default_privacy_profile, mode) VALUES (?, ?, 'active', 'open', 'live_safe')",
            (sid, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO audio_streams (id, session_id, role, source_language, target_language, enabled) VALUES (?, ?, 'microphone', 'ru', 'en', 1)",
            (stream_id, sid),
        )

    await db.write(_tx)
    return sid, stream_id


async def _insert_segment(db, session_id, stream_id, track, t_start, t_end, **kw):
    seg_id = uuid.uuid4().hex
    now = "2025-01-01T00:00:00.000"

    def _tx(conn):
        conn.execute(
            """INSERT OR IGNORE INTO segments
               (id, session_id, stream_id, t_start_ms, t_end_ms,
                privacy_profile, track, created_at)
               VALUES (?, ?, ?, ?, ?, 'open', ?, ?)""",
            (seg_id, session_id, stream_id, t_start, t_end, track, now),
        )

    await db.write(_tx)
    return seg_id


class TestExportView:

    @pytest.mark.asyncio
    async def test_export_view_returns_only_accurate(self, db):
        sid, stream_id = await _seed_session_stream(db)
        acc_id = await _insert_segment(
            db, sid, stream_id, "accurate", 0, 1000
        )
        await _insert_segment(db, sid, stream_id, "fast", 0, 800)
        await _insert_segment(db, sid, stream_id, "fast", 200, 900)

        svc = SupersedeService(db)
        rows = await svc.export_view(sid)

        assert len(rows) == 1
        assert rows[0]["id"] == acc_id
        assert rows[0]["track"] == "accurate"

    @pytest.mark.asyncio
    async def test_export_view_empty_when_only_fast(self, db):
        sid, stream_id = await _seed_session_stream(db)
        await _insert_segment(db, sid, stream_id, "fast", 0, 800)
        await _insert_segment(db, sid, stream_id, "fast", 200, 900)

        svc = SupersedeService(db)
        rows = await svc.export_view(sid)
        assert len(rows) == 0


class TestSupersedeChain:

    @pytest.mark.asyncio
    async def test_link_supersedes_matching_fast(self, db):
        sid, stream_id = await _seed_session_stream(db)
        fast_id = await _insert_segment(
            db, sid, stream_id, "fast", 100, 800
        )
        acc_id = await _insert_segment(
            db, sid, stream_id, "accurate", 0, 1000
        )

        svc = SupersedeService(db, SupersedeConfig(min_overlap=0.3))
        result = await svc.link(acc_id)

        assert result.accurate_id == acc_id
        assert fast_id in result.superseded_fast_ids

    @pytest.mark.asyncio
    async def test_link_skips_non_overlapping(self, db):
        sid, stream_id = await _seed_session_stream(db)
        await _insert_segment(
            db, sid, stream_id, "fast", 5000, 6000
        )
        acc_id = await _insert_segment(
            db, sid, stream_id, "accurate", 0, 1000
        )

        svc = SupersedeService(db, SupersedeConfig(min_overlap=0.3))
        result = await svc.link(acc_id)

        assert acc_id not in result.superseded_fast_ids
