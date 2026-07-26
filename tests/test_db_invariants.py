from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from app.db import Database, DbConfig
from app.errors import ImmutableFieldError


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


async def _seed(db, **kw) -> str:
    import uuid

    seg_id = uuid.uuid4().hex
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
        conn.execute(
            "INSERT OR IGNORE INTO segments (id, session_id, stream_id, t_start_ms, t_end_ms, privacy_profile, track, created_at) VALUES (?, ?, ?, 0, 1000, 'open', 'accurate', ?)",
            (seg_id, sid, stream_id, now),
        )

    await db.write(_tx)
    return seg_id


class TestRawTextImmutable:

    @pytest.mark.asyncio
    async def test_raw_text_set_first_time(self, db):
        seg_id = await _seed(db)
        await db.execute(
            "UPDATE segments SET raw_text = ? WHERE id = ?", ("hello", seg_id)
        )
        row = await db.fetch_one(
            "SELECT raw_text FROM segments WHERE id = ?", (seg_id,)
        )
        assert row["raw_text"] == "hello"

    @pytest.mark.asyncio
    async def test_raw_text_immutable_on_second_update(self, db):
        seg_id = await _seed(db)
        await db.execute(
            "UPDATE segments SET raw_text = ? WHERE id = ?", ("first", seg_id)
        )
        with pytest.raises(ImmutableFieldError) as exc:
            await db.execute(
                "UPDATE segments SET raw_text = ? WHERE id = ?",
                ("second", seg_id),
            )
        assert "raw_text" in str(exc.value)


class TestPrivacyProfileImmutable:

    @pytest.mark.asyncio
    async def test_privacy_profile_cannot_change(self, db):
        seg_id = await _seed(db)
        with pytest.raises(ImmutableFieldError) as exc:
            await db.execute(
                "UPDATE segments SET privacy_profile = 'confidential' WHERE id = ?",
                (seg_id,),
            )
        assert "privacy_profile" in str(exc.value)


class TestSingleSqlite3Connect:

    def test_only_db_py_imports_sqlite3_connect(self):
        app_root = Path(__file__).parent.parent / "app"
        matches = []
        for py in sorted(app_root.rglob("*.py")):
            if py.name == "__init__.py":
                continue
            if "sqlite3.connect(" in py.read_text():
                rel = str(py.relative_to(app_root.parent))
                matches.append(rel)
        assert len(matches) == 1, f"sqlite3.connect found outside db.py: {matches}"
        assert "app/db.py" in matches[0]


class TestWriterQueue:

    @pytest.mark.asyncio
    async def test_write_returns_result(self, db):
        result = await db.write(lambda c: 42)
        assert result == 42

    @pytest.mark.asyncio
    async def test_write_exception_goes_to_failed(self, db):
        with pytest.raises(ValueError):
            await db.write(lambda c: (_ for _ in ()).throw(ValueError("boom")))
