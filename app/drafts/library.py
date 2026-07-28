"""app/drafts/library.py — I1 fact library."""

from __future__ import annotations

import datetime
import math
import uuid
from dataclasses import dataclass
from typing import List

from app.db import Database
from app.errors import InvariantViolation


@dataclass(frozen=True, slots=True)
class LibraryContext:
    id: str
    name: str
    domain: str | None
    content_text: str
    token_estimate: int
    updated_at: str  # ISO-8601 UTC


class LibraryTooLarge(InvariantViolation):
    """Raised when attempting to upsert a library entry that exceeds max_tokens.

    The exception's args contain (estimate, limit) as integers.
    """

    def __init__(self, estimate: int, limit: int) -> None:
        super().__init__(f"library_too_large: estimate={estimate}, limit={limit}")
        self.estimate = estimate
        self.limit = limit


class FactLibrary:
    def __init__(self, db: Database, *, max_tokens: int = 30000) -> None:
        self._db = db
        self._max_tokens = max_tokens
        self._cache: dict[str, LibraryContext] = {}

    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        # Count Cyrillic characters (Unicode block U+0400–U+04FF)
        cyrillic_count = sum(1 for ch in text if '\u0400' <= ch <= '\u04FF')
        total = len(text)
        share = cyrillic_count / total if total else 0.0
        # Formula: tokens ≈ len * (cyr_share / 2.5 + (1 - cyr_share) / 4)
        return math.ceil(total * (share / 2.5 + (1 - share) / 4))

    def check_limit(self, text: str, max_tokens: int | None = None) -> tuple[bool, int]:
        limit = max_tokens if max_tokens is not None else self._max_tokens
        estimate = self.estimate_tokens(text)
        return estimate <= limit, estimate

    async def list(self) -> List[LibraryContext]:
        rows = await self._db.fetch_all(
            """
            SELECT id, name, domain, token_estimate, updated_at
            FROM library_contexts
            ORDER BY name
            """
        )
        return [
            LibraryContext(
                id=row["id"],
                name=row["name"],
                domain=row["domain"],
                content_text="",  # not loaded in list
                token_estimate=row["token_estimate"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def get(self, context_id: str) -> LibraryContext:
        ver = await self._db.fetch_one(
            "SELECT updated_at FROM library_contexts WHERE id = ?",
            (context_id,),
        )
        if not ver:
            self._cache.pop(context_id, None)
            raise InvariantViolation("library_context_not_found")
        cached = self._cache.get(context_id)
        if cached is not None and cached.updated_at == ver["updated_at"]:
            return cached

        row = await self._db.fetch_one(
            """
            SELECT id, name, domain, content_text, token_estimate, updated_at
            FROM library_contexts
            WHERE id = ?
            """,
            (context_id,),
        )
        if not row:
            self._cache.pop(context_id, None)
            raise InvariantViolation("library_context_not_found")
        ctx = LibraryContext(
            id=row["id"],
            name=row["name"],
            domain=row["domain"],
            content_text=row["content_text"],
            token_estimate=row["token_estimate"],
            updated_at=row["updated_at"],
        )
        self._cache[context_id] = ctx
        return ctx

    async def upsert(self, name: str, domain: str | None, content_text: str) -> str:
        # Validate name
        if not name or not name.strip():
            raise InvariantViolation("library_name_empty")
        name = name.strip()
        # Normalize text on write: strip BOM, normalize line endings, trim trailing spaces per line, trim outer whitespace
        normalized = self._normalize(content_text)
        estimate = self.estimate_tokens(normalized)
        if estimate > self._max_tokens:
            raise LibraryTooLarge(estimate, self._max_tokens)
        # Upsert by name (unique)
        existing = await self._db.fetch_one(
            "SELECT id FROM library_contexts WHERE name = ?", (name,)
        )
        now = self._now_iso()
        if existing:
            cid = existing["id"]
            await self._db.execute(
                """
                UPDATE library_contexts
                SET domain = ?, content_text = ?, token_estimate = ?, updated_at = ?
                WHERE id = ?
                """,
                (domain, normalized, estimate, now, cid),
            )
        else:
            cid = uuid.uuid4().hex
            await self._db.execute(
                """
                INSERT INTO library_contexts (id, name, domain, content_text, token_estimate, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cid, name, domain, normalized, estimate, now),
            )
        self._cache.pop(cid, None)
        return cid

    async def delete(self, context_id: str) -> None:
        # Check if any session uses this library context
        used = await self._db.fetch_one(
            """
            SELECT 1 FROM sessions WHERE library_context_id = ? LIMIT 1
            """,
            (context_id,),
        )
        if used:
            raise InvariantViolation("library_context_in_use")
        await self._db.execute(
            "DELETE FROM library_contexts WHERE id = ?", (context_id,)
        )
        self._cache.pop(context_id, None)

    @staticmethod
    def _normalize(text: str) -> str:
        # Remove BOM if present
        if text.startswith("\ufeff"):
            text = text[1:]
        # Normalize line endings: replace CRLF and CR with LF
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Remove trailing spaces from each line
        lines = [line.rstrip() for line in text.splitlines()]
        # Join back with LF
        text = "\n".join(lines)
        # Strip leading and trailing whitespace of the whole document
        return text.strip()

    def _now_iso(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')