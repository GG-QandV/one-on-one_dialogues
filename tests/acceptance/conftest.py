"""tests/acceptance/conftest.py — фикстуры окружения для самопроверки стенда H2."""

from __future__ import annotations

import pytest

from tests.acceptance.harness import CANARY_KEY, CheckEnv, _new_env

__all__ = ["CANARY_KEY", "check_env"]


@pytest.fixture
async def check_env() -> CheckEnv:
    env = await _new_env(0)
    try:
        yield env
    finally:
        await env.db.close()
