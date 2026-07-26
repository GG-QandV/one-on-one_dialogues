"""Tests for clipboard delivery (G3)."""

import asyncio
import os

import pytest

from app.delivery.clipboard import (
    Backend,
    ClipboardConfig,
    copy,
    detect_backend,
    diagnose,
)


class TestDetectBackend:
    def test_wayland_detected(self, monkeypatch):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("DISPLAY", "")
        async def test():
            cfg = ClipboardConfig()
            backend = await detect_backend(cfg)
            # wl-copy may not be installed, so it could fall to NONE
            return backend
        backend = asyncio.run(test())
        # If wl-copy is available, it's WL_COPY; otherwise NONE (Wayland detected but no tool)
        # Both are valid outcomes — we just check that X11 backends aren't selected
        assert backend in (Backend.WL_COPY, Backend.NONE)

    def test_x11_detected(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")
        async def test():
            cfg = ClipboardConfig()
            return await detect_backend(cfg)
        backend = asyncio.run(test())
        assert backend in (Backend.XCLIP, Backend.XSEL, Backend.NONE)

    def test_no_display(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        async def test():
            cfg = ClipboardConfig()
            return await detect_backend(cfg)
        backend = asyncio.run(test())
        assert backend == Backend.NONE

    def test_explicit_backend(self, monkeypatch):
        async def test():
            cfg = ClipboardConfig(backend=Backend.NONE)
            return await detect_backend(cfg)
        backend = asyncio.run(test())
        assert backend == Backend.NONE


class TestCopy:
    def test_empty_text_returns_false(self):
        async def test():
            result = await copy("", ClipboardConfig(backend=Backend.NONE))
            return result
        assert asyncio.run(test()) is False

    def test_none_backend_returns_false(self):
        async def test():
            result = await copy("hello", ClipboardConfig(backend=Backend.NONE))
            return result
        assert asyncio.run(test()) is False


class TestDiagnose:
    def test_diagnose_returns_dict(self):
        result = diagnose()
        assert isinstance(result, dict)
        assert "backends" in result
        assert "found" in result
        assert "hint" in result

    def test_diagnose_returns_expected_keys(self):
        result = diagnose()
        assert isinstance(result["found"], list)
        assert isinstance(result["hint"], str)
