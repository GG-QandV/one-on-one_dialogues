"""app/security/redactor.py — G1 log redactor."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any


DEFAULT_PATTERNS: tuple[str, ...] = (
    r"sk-[A-Za-z0-9]{20,}",  # OpenAI style
    r"AIza[0-9A-Za-z\-_]{35}",  # Google API key
    r"ya29\.[0-9A-Za-z\-_]+",  # Google OAuth
    r"SG\.[0-9A-Za-z\-_]{22,}",  # SendGrid
    r"Bearer\s+[A-Za-z0-9\-_=.]+",  # Bearer token
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",  # UUID (maybe too broad, but keep as example)
)
MASK = "***REDACTED***"


class LogRedactor(logging.Filter):
    """
    Logging redactor that scrubs secrets from log records.
    """

    def __init__(self, patterns: Iterable[str] | None = None, *, mask: str = MASK) -> None:
        super().__init__()
        self._mask = mask
        self._patterns: list[re.Pattern[str]] = []
        self._literals: set[str] = set()
        self._stats = {
            "patterns": 0,
            "literals": 0,
            "total": 0,
        }
        if patterns is None:
            patterns = DEFAULT_PATTERNS
        for pat in patterns:
            self.add_pattern(pat)

    def add_pattern(self, regex: str) -> None:
        """Add a regex pattern to match and redact."""
        self._patterns.append(re.compile(regex))

    def add_literal(self, secret: str) -> None:
        """Add a literal string to redact (exact match)."""
        self._literals.add(secret)

    def remove_literal(self, secret: str) -> None:
        """Remove a literal from the set."""
        self._literals.discard(secret)

    def redact(self, text: str) -> str:
        """Return a copy of ``text`` with secrets replaced by ``self._mask``."""
        if not isinstance(text, str):
            # If it's not a string, return as-is (should not happen in log records)
            return text
        result = text
        # Apply patterns
        for pat in self._patterns:
            # We need to count replacements for stats? We'll just do it.
            # But we also want to avoid overlapping replacements.
            # Simpler: iterate over matches and replace from end to start.
            # However, for simplicity and given the low volume, we do:
            new_result, n = pat.subn(self._mask, result)
            if n:
                self._stats["patterns"] += n
                result = new_result
        # Apply literals
        for lit in self._literals:
            if lit in result:
                # Replace all occurrences
                count = result.count(lit)
                result = result.replace(lit, self._mask)
                self._stats["literals"] += count
        return result

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Redact the record in-place and return True (so the record is logged).
        We redact msg, args, exc_text, and exc_info.
        """
        # Reduce the chance of double counting if filter is called multiple times?
        # We'll just redact each time.
        # Message
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        # Args
        if isinstance(record.args, tuple):
            new_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    new_args.append(self.redact(arg))
                else:
                    new_args.append(arg)
            record.args = tuple(new_args)
        elif isinstance(record.args, dict):
            new_args = {}
            for k, v in record.args.items():
                if isinstance(v, str):
                    new_args[k] = self.redact(v)
                else:
                    new_args[k] = v
            record.args = new_args
        # Exc text (already formatted traceback)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        # Exc info (exception tuple) -> we need to format it, redact, and put back as text
        if record.exc_info:
            # exc_info is (type, value, traceback) or None
            # We'll format it to string, redact, and set as exc_text, then clear exc_info
            # to prevent the formatter from re-formatting.
            import traceback
            if isinstance(record.exc_info, tuple) and len(record.exc_info) == 3:
                # Format the exception
                formatted = "".join(traceback.format_exception(*record.exc_info))
                redacted = self.redact(formatted)
                record.exc_text = redacted
                # Clear exc_info so that the formatter doesn't try to format it again
                record.exc_info = None
            else:
                # If it's not a tuple, we still try to redact as string? Safer to just set exc_text.
                if isinstance(record.exc_info, str):
                    record.exc_text = self.redact(record.exc_info)
                record.exc_info = None
        # Update stats for any redactions in this call? We already counted in redact.
        return True

    def snapshot(self) -> dict[str, Any]:
        """Return a snapshot of the redactor's state (without secrets)."""
        return {
            "patterns": [p.pattern for p in self._patterns],
            "literals_count": len(self._literals),
            "stats": self._stats.copy(),
        }