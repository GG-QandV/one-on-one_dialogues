"""Clipboard delivery — copy text to system clipboard.

Единственный путь, которым текст покидает процесс в MVP.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class Backend(str, Enum):
    WL_COPY = "wl-copy"
    XCLIP = "xclip"
    XSEL = "xsel"
    NONE = "none"


ARGS: dict[Backend, tuple[str, ...]] = {
    Backend.WL_COPY: ("wl-copy",),
    Backend.XCLIP: ("xclip", "-selection", "clipboard"),
    Backend.XSEL: ("xsel", "--clipboard", "--input"),
}


@dataclass(frozen=True, slots=True)
class ClipboardConfig:
    timeout_s: float = 3.0
    backend: Backend | None = None


_backend_cache: Backend | None = None


async def detect_backend(cfg: ClipboardConfig) -> Backend:
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache
    if cfg.backend is not None:
        _backend_cache = cfg.backend
        return _backend_cache
    if os.environ.get("WAYLAND_DISPLAY"):
        if shutil.which("wl-copy"):
            _backend_cache = Backend.WL_COPY
            return _backend_cache
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            _backend_cache = Backend.XCLIP
            return _backend_cache
        if shutil.which("xsel"):
            _backend_cache = Backend.XSEL
            return _backend_cache
    _backend_cache = Backend.NONE
    return _backend_cache


async def _run_backend(backend: Backend, text: str, timeout_s: float) -> bool:
    args = ARGS.get(backend)
    if args is None:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.communicate(text.encode()), timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0
    except FileNotFoundError:
        return False


async def copy(text: str, cfg: ClipboardConfig | None = None) -> bool:
    if not text:
        return False
    if cfg is None:
        cfg = ClipboardConfig()
    backend = await detect_backend(cfg)
    if backend == Backend.NONE:
        return False
    return await _run_backend(backend, text, cfg.timeout_s)


async def copy_draft(
    guard: Any, draft_id: str, text: str, cfg: ClipboardConfig | None = None
) -> bool:
    ok = await copy(text, cfg)
    if ok:
        await guard.mark(draft_id, "copied")
    return ok


def diagnose() -> Dict[str, Any]:
    backends_found: list[str] = []
    for b in (Backend.WL_COPY, Backend.XCLIP, Backend.XSEL):
        if shutil.which(b.value):
            backends_found.append(b.value)
    hint_parts: list[str] = []
    if not backends_found:
        hint_parts.append("apt install wl-clipboard")
        hint_parts.append("apt install xclip")
    hint = " или ".join(hint_parts) if hint_parts else "буфер обмена работает"
    return {
        "backends": [b.value for b in Backend],
        "found": backends_found,
        "hint": hint,
    }
