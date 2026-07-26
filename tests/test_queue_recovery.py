from __future__ import annotations

import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database, DbConfig
from app.errors import LeaseLost
from app.privacy import PrivacyController, PrivacyProfile
from app.queue import JobQueue, JobType, QueueConfig


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
async def queue(db) -> JobQueue:
    privacy = PrivacyController(PrivacyProfile.OPEN)
    cfg = QueueConfig(lease_ttl_s=5_000_000)
    q = JobQueue(db, privacy, cfg)
    return q


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


class TestRecover:

    @pytest.mark.asyncio
    async def test_recover_requeues_expired_idempotent(self, db, queue):
        jid = uuid.uuid4().hex
        now = datetime.now(UTC)

        def _insert(conn):
            conn.execute(
                """INSERT INTO jobs (id, type, payload_json, status,
                  idempotent, attempts, max_attempts,
                  privacy_profile, privacy_gen, created_at, updated_at,
                  lease_owner, lease_expires_at)
                  VALUES (?, 'translate', '{}', 'running',
                          1, 1, 3,
                          'open', 0, ?, ?, 'dead-owner', ?)""",
                (jid, _iso(now), _iso(now), _iso(now - timedelta(hours=1))),
            )

        await db.write(_insert)
        result = await queue.recover()

        assert result["requeued"] == 1
        assert result["failed"] == 0
        row = await db.fetch_one(
            "SELECT status FROM jobs WHERE id = ?", (jid,)
        )
        assert row["status"] == "queued"

    @pytest.mark.asyncio
    async def test_recover_fails_non_idempotent(self, db, queue):
        jid = uuid.uuid4().hex
        now = datetime.now(UTC)

        def _insert(conn):
            conn.execute(
                """INSERT INTO jobs (id, type, payload_json, status,
                  idempotent, attempts, max_attempts,
                  privacy_profile, privacy_gen, created_at, updated_at,
                  lease_owner, lease_expires_at)
                  VALUES (?, 'export', '{}', 'running',
                          0, 1, 3,
                          'open', 0, ?, ?, 'dead-owner', ?)""",
                (jid, _iso(now), _iso(now), _iso(now - timedelta(hours=1))),
            )

        await db.write(_insert)
        result = await queue.recover()

        assert result["requeued"] == 0
        assert result["failed"] == 1
        row = await db.fetch_one(
            "SELECT status FROM jobs WHERE id = ?", (jid,)
        )
        assert row["status"] == "failed"


class TestIdempotentEnqueue:

    @pytest.mark.asyncio
    async def test_same_key_returns_same_id(self, queue):
        id1 = await queue.enqueue(JobType.TRANSLATE, idempotency_key="dup-key")
        id2 = await queue.enqueue(JobType.TRANSLATE, idempotency_key="dup-key")
        assert id1 == id2

    @pytest.mark.asyncio
    async def test_different_keys_different_ids(self, queue):
        id1 = await queue.enqueue(JobType.TRANSLATE, idempotency_key="key-a")
        id2 = await queue.enqueue(JobType.TRANSLATE, idempotency_key="key-b")
        assert id1 != id2


class TestRaceSafety:

    @pytest.mark.asyncio
    async def test_claim_is_atomic(self, queue):
        jid = await queue.enqueue(JobType.TRANSLATE)
        job1 = await queue._claim(JobType.TRANSLATE)
        assert job1 is not None
        assert job1.id == jid
        assert job1.status.name == "RUNNING"

        job2 = await queue._claim(JobType.TRANSLATE)
        assert job2 is None
