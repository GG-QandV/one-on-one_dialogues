"""Tests for app/watchdog/memory.py (F1)."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.watchdog.memory import (
    MemoryMonitor,
    MemoryConfig,
    MemorySample,
)


def _fake_cgroup_v2_line(path: str) -> str:
    """Return a fake /proc/self/cgroup line for a given cgroup path."""
    return f"0::/user.slice/{path.strip('/')}"


class TestCgroupV2:
    """MemoryMonitor with cgroup v2 source."""

    def test_source_is_cgroup_when_v2_available(self, monkeypatch):
        monkeypatch.setattr(
            Path, "read_text",
            lambda self: _fake_cgroup_v2_line("speech.service")
            if str(self) == "/proc/self/cgroup"
            else "1234567890\n",
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)
        m = MemoryMonitor()
        assert m.source == "cgroup"
        assert m.degraded_source is False

    def test_current_mb_after_read(self, monkeypatch):
        monkeypatch.setattr(
            Path, "read_text",
            lambda self: _fake_cgroup_v2_line("speech.service")
            if str(self) == "/proc/self/cgroup"
            else str(500 << 20),  # 500 MB in bytes
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)
        m = MemoryMonitor()
        s = m.read()
        assert abs(s.total_mb - 500.0) < 1.0
        assert s.source == "cgroup"

    def test_current_mb_no_io(self, monkeypatch):
        """current_mb() does not do I/O (cached)."""
        monkeypatch.setattr(
            Path, "read_text",
            lambda self: _fake_cgroup_v2_line("speech.service")
            if str(self) == "/proc/self/cgroup"
            else str(500 << 20),
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)
        m = MemoryMonitor()
        m._tick()  # sets _last_sample

        calls = []

        def fail_read(_):
            calls.append(1)
            raise OSError("should not be called")

        monkeypatch.setattr(Path, "read_text", fail_read)
        val = m.current_mb()
        assert abs(val - 500.0) < 1.0
        assert len(calls) == 0  # no new I/O


class TestCgroupV1:
    """Fallback to cgroup v1 when v2 unavailable."""

    def test_fallback_to_v1(self, monkeypatch):
        original_read = Path.read_text

        def fake_read(self):
            s = str(self)
            if s == "/proc/self/cgroup":
                raise OSError("no cgroup v2")
            if s == "/sys/fs/cgroup/memory/memory.usage_in_bytes":
                return str(600 << 20)
            if s == "/proc/self/stat":
                return "12345 (python) S 0 0 0 0"
            return original_read(self)

        monkeypatch.setattr(Path, "read_text", fake_read)

        def fake_exists(self):
            s = str(self)
            if "memory.current" in s:
                return False
            if "memory.usage_in_bytes" in s:
                return True
            return False

        monkeypatch.setattr(Path, "exists", fake_exists)

        m = MemoryMonitor()
        assert m.source == "cgroup"
        s = m.read()
        assert abs(s.total_mb - 600.0) < 1.0

    def test_v1_also_unavailable_falls_to_vmrss(self, monkeypatch):
        original_read = Path.read_text

        def fake_read(self):
            s = str(self)
            if s == "/proc/self/cgroup":
                raise OSError("no cgroup")
            if "memory.usage_in_bytes" in str(self) or "memory.current" in str(self):
                raise OSError("no file")
            if s == "/proc/self/stat":
                return "12345 (python) S 0 0 0 0"
            if "VmRSS:" in str(self) or "status" in str(self):
                return "Name: python\nVmRSS:    250000 kB\nPPid: 0\n"
            return original_read(self)

        monkeypatch.setattr(Path, "read_text", fake_read)
        monkeypatch.setattr(Path, "exists", lambda self: False)

        m = MemoryMonitor()
        assert m.source == "vmrss"
        assert m.degraded_source is True
        s = m.read()
        assert abs(s.total_mb - 244.0) < 5.0  # ~250000/1024 ≈ 244 MB


class TestHistoryAndPeak:
    """History buffer and peak memory tracking."""

    def test_history_grows_and_bounded(self, monkeypatch):
        monkeypatch.setattr(
            Path, "read_text",
            lambda self: _fake_cgroup_v2_line("test.service")
            if str(self) == "/proc/self/cgroup"
            else "100000000\n",
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)

        cfg = MemoryConfig(history_size=5)
        m = MemoryMonitor(config=cfg)

        for _ in range(7):
            m._tick()

        hist = m.history()
        assert len(hist) == 5  # bounded
        assert all(isinstance(h, MemorySample) for h in hist)

    def test_history_returns_copy(self, monkeypatch):
        monkeypatch.setattr(
            Path, "read_text",
            lambda self: _fake_cgroup_v2_line("test.service")
            if str(self) == "/proc/self/cgroup"
            else "100000000\n",
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)

        m = MemoryMonitor()
        m._tick()
        hist = m.history()
        # Mutate the result
        mutable = list(hist)
        mutable.clear()
        # Internal state should be unchanged
        assert len(m.history()) == 1

    def test_peak_mb_tracks_maximum(self, monkeypatch):
        values = iter(["100000000", "500000000", "200000000"])

        def fake_read(self):
            s = str(self)
            if s == "/proc/self/cgroup":
                return _fake_cgroup_v2_line("test.service")
            if "memory.current" in s:
                return next(values)
            return ""

        monkeypatch.setattr(Path, "read_text", fake_read)
        monkeypatch.setattr(Path, "exists", lambda self: True)

        m = MemoryMonitor()
        m._tick()  # 95 MB
        m._tick()  # 477 MB
        m._tick()  # 191 MB
        assert abs(m.peak_mb() - 477.0) < 5.0

    def test_history_n_param(self, monkeypatch):
        monkeypatch.setattr(
            Path, "read_text",
            lambda self: _fake_cgroup_v2_line("test.service")
            if str(self) == "/proc/self/cgroup"
            else "100000000\n",
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)

        m = MemoryMonitor()
        for _ in range(10):
            m._tick()

        assert len(m.history(3)) == 3
        assert len(m.history()) == 10


class TestVmrssChildren:
    """Children process summing in vmrss mode."""

    def test_children_are_summed(self, monkeypatch):
        from app.watchdog.memory import _find_children, _read_vmrss

        # Force vmrss mode by making all cgroup paths unavailable
        monkeypatch.setattr(Path, "read_text", lambda self: (
            "" if "VmRSS:" in str(self) else
            (_ for _ in ()).throw(OSError("no cgroup"))
        ))
        monkeypatch.setattr(Path, "exists", lambda self: False)

        # Mock children discovery
        monkeypatch.setattr(
            "app.watchdog.memory._find_children",
            lambda ppid: [200, 300] if ppid == 100 else [400] if ppid == 200 else [],
        )
        monkeypatch.setattr(
            "app.watchdog.memory._read_vmrss",
            lambda pid: {
                100: 100.0,
                200: 300.0,
                300: 50.0,
                400: 25.0,
            }.get(pid, 0.0),
        )
        monkeypatch.setattr(
            "app.watchdog.memory._my_pid",
            lambda: 100,
        )

        m = MemoryMonitor()
        s = m.read()
        assert m.source == "vmrss"
        # self: pid 100 = 100 MB
        # children tree: 200(300) + 300(50) + 400(25 via 200) = 375 MB
        assert abs(s.self_mb - 100.0) < 1.0
        assert abs(s.children_mb - 375.0) < 1.0
        assert abs(s.total_mb - 475.0) < 5.0


class TestUnavailable:
    """All sources unavailable."""

    def test_all_unavailable_returns_zero(self, monkeypatch):
        def fake_read(self):
            raise OSError("no access")

        monkeypatch.setattr(Path, "read_text", fake_read)
        monkeypatch.setattr(Path, "exists", lambda self: False)

        m = MemoryMonitor()
        s = m.read()
        assert s.total_mb == 0.0
        snap = m.snapshot()
        # Should not crash
        assert "available" in snap


class TestSnapshot:
    """Snapshot contents."""

    def test_snapshot_keys(self, monkeypatch):
        monkeypatch.setattr(
            Path, "read_text",
            lambda self: _fake_cgroup_v2_line("speech.service")
            if str(self) == "/proc/self/cgroup"
            else str(300 << 20),
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)

        m = MemoryMonitor()
        m._tick()
        snap = m.snapshot()
        assert "source" in snap
        assert "memory_mb" in snap
        assert "peak_mb" in snap
        assert "history_size" in snap
        assert "available" in snap

    def test_snapshot_periodic_warning(self, monkeypatch):
        """WARNING emitted once on vmrss start."""
        import logging
        from io import StringIO

        logger = logging.getLogger("app.watchdog.memory")
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)

        def fake_read(self):
            s = str(self)
            if "cgroup" in s or "memory" in s:
                raise OSError("no cgroup")
            if s == "/proc/self/stat":
                return "12345 (python) S 0 0 0 0"
            if "VmRSS:" in s:
                return "Name: python\nVmRSS:    100000 kB\nPPid: 0\n"
            return ""

        monkeypatch.setattr(Path, "read_text", fake_read)
        monkeypatch.setattr(Path, "exists", lambda self: False)

        m = MemoryMonitor()
        logger.removeHandler(handler)
        assert buf.getvalue() != ""
        assert "cgroup" in buf.getvalue() or "VmRSS" in buf.getvalue()


class TestStartStop:
    """Async start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_stop(self, monkeypatch):
        monkeypatch.setattr(
            Path, "read_text",
            lambda self: _fake_cgroup_v2_line("test.service")
            if str(self) == "/proc/self/cgroup"
            else "100000000\n",
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)

        m = MemoryMonitor()
        await m.start()
        assert m._task is not None
        await m.stop()
        assert m._task is None

    @pytest.mark.asyncio
    async def test_double_stop_safe(self, monkeypatch):
        monkeypatch.setattr(
            Path, "read_text",
            lambda self: _fake_cgroup_v2_line("test.service")
            if str(self) == "/proc/self/cgroup"
            else "100000000\n",
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)

        m = MemoryMonitor()
        await m.start()
        await m.stop()
        await m.stop()  # second call safe


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
