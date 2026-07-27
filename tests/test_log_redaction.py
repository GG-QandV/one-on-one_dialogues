"""Tests for app/security/redactor.py (G1)."""

from __future__ import annotations

import logging

import pytest

from app.security.redactor import LogRedactor


class TestLogRedactorIdempotence:
    """G1: идемпотентность фильтра — двойной вызов не портит вывод и не задваивает счёт."""

    def test_redact_twice_same_result(self):
        """Повторный redact той же строки даёт тот же результат."""
        redactor = LogRedactor()
        text = "my key is sk-abc123xyz456def789012 and it's secret"
        first = redactor.redact(text)
        second = redactor.redact(first)
        assert first == second
        assert "sk-abc123xyz456def789012" not in first
        assert "sk-abc123xyz456def789012" not in second
        assert "***REDACTED***" in first

    def test_filter_logrecord_twice_no_corruption(self):
        """filter на одном LogRecord дважды — сообщение не портится, маскировка корректна."""
        redactor = LogRedactor()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg="token sk-abc123xyz456def789012 used",
            args=(),
            exc_info=None,
        )
        redactor.filter(record)
        first_msg = record.msg
        redactor.filter(record)
        second_msg = record.msg
        assert first_msg == second_msg
        assert "sk-abc123xyz456def789012" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_stats_no_double_count_on_second_call(self):
        """snapshot() после двух filter показывает количество заматченных
        паттернов без удвоения — второй filter не добавляет новых секретов."""
        redactor = LogRedactor()
        redactor.add_literal("secret1")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg="contains secret1 and secret1 again",
            args=(),
            exc_info=None,
        )
        redactor.filter(record)
        snap1 = redactor.snapshot()
        redactor.filter(record)
        snap2 = redactor.snapshot()
        # Во втором вызове секретов не осталось — статистика не растёт
        assert "***REDACTED***" in record.msg
        # После второго вызова literal count не должен удвоиться —
        # секретов в тексте уже нет, но redact вызывается снова.
        # По текущей реализации: redact заменяет не-найденное -> 0 замен,
        # поэтому stats["literals"] не растёт.
        assert snap2["stats"]["literals"] == snap1["stats"]["literals"]

    def test_redact_preserves_non_secret_content(self):
        """redact не меняет текст, не содержащий секретов."""
        redactor = LogRedactor()
        clean = "Hello, this is a normal log message without secrets."
        result = redactor.redact(clean)
        assert result == clean

    def test_redact_literal_roundtrip(self):
        """literal заменяется и при повторном проходе не дублируется маска."""
        redactor = LogRedactor()
        redactor.add_literal("my-api-key")
        text = "my-api-key is used"
        result = redactor.redact(text)
        assert "my-api-key" not in result
        assert result.count("***REDACTED***") == 1
        # Второй проход
        result2 = redactor.redact(result)
        assert result2 == result
