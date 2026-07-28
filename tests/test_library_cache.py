"""Tests for FactLibrary cache (I1)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from app.db import Database, DbConfig
from app.drafts.library import FactLibrary


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


@pytest.fixture
async def lib(db):
    return FactLibrary(db)


class TestCache:
    @pytest.mark.asyncio
    async def test_second_get_uses_cache(self, db, lib):
        """Два get подряд без изменений — второй из кэша (один SELECT на проверку версии)."""
        cid = uuid.uuid4().hex
        now = "2025-01-01T00:00:00.000"
        await db.execute(
            "INSERT INTO library_contexts (id, name, domain, content_text, token_estimate, updated_at) "
            "VALUES (?, ?, NULL, 'fact один', 10, ?)",
            (cid, "test1", now),
        )

        ctx1 = await lib.get(cid)
        assert ctx1.content_text == "fact один"

        ctx2 = await lib.get(cid)
        assert ctx2 is ctx1  # same cached object

    @pytest.mark.asyncio
    async def test_upsert_invalidates_cache(self, db, lib):
        """upsert того же раздела → следующий get перечитывает (updated_at сменился)."""
        cid = await lib.upsert("test2", None, "fact два")
        ctx1 = await lib.get(cid)
        assert ctx1.content_text == "fact два"

        await lib.upsert("test2", None, "fact два updated")
        ctx2 = await lib.get(cid)
        assert ctx2 is not ctx1  # new object
        assert ctx2.content_text == "fact два updated"

    @pytest.mark.asyncio
    async def test_delete_clears_cache(self, db, lib):
        """Удалённый раздел → get бросает, кэш очищен."""
        cid = await lib.upsert("test3", None, "fact три")
        ctx = await lib.get(cid)
        assert ctx.content_text == "fact три"

        await lib.delete(cid)

        from app.errors import InvariantViolation
        with pytest.raises(InvariantViolation, match="library_context_not_found"):
            await lib.get(cid)
