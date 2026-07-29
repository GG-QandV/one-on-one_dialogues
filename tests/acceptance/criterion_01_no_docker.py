"""Критерий 1 (§21): запуск без Docker/GPU/тяжёлого ML на чистой машине.

LIVE — требует установки на чистую Mint (скрипты B7) и фиксации версий
вручную. Автоматизировать нельзя по определению критерия.
"""

from __future__ import annotations

from tests.acceptance.harness import live_stub

CHECK = live_stub(
    1,
    "запуск без Docker/GPU/тяжёлого ML",
    "Руками: установить на чистую Linux Mint по README/scripts/install_whispercpp.sh, "
    "зафиксировать версии (python, whisper.cpp, зависимости), подтвердить отсутствие "
    "Docker/GPU в стеке.",
)
