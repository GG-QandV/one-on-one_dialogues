"""tests/test_stt_provider.py — цепочка фолбэков STT (REFACTOR_STT_fallback_chain).

Покрывает: фабрику цепочки, поведение failover/cooldown, инвариант §8.7
(local_whisper без cooldown и последним), запись провайдера в результат.
Конфиг/контракт/HTTP — в `test_stt_provider_architecture.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import (
    SttChainEntry,
    SttSection,
    default_stt_section,
)
from app.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseInvalid,
    ProviderUnavailable,
)
from app.privacy import PrivacyController, PrivacyProfile
from app.stt.base import SttRequest, SttResult
from app.stt.chain import ChainEntry, SttChainExhausted, SttFailoverChain
from app.stt.cloud_api import CloudSttProvider
from app.stt.factory import build_stt_chain, resolve_model_path


def _req() -> SttRequest:
    return SttRequest(audio_path=Path("/dev/null"), segment_id="s1")


class FakeProvider:
    """Управляемый двойник SttProvider."""

    def __init__(self, name: str, *, result: SttResult | None = None, exc=None):
        self.name = name
        self._result = result or SttResult(raw_text=f"via {name}", model="m")
        self._exc = exc
        self.calls = 0

    async def transcribe(self, req, *, fence=None) -> SttResult:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result

    async def close(self) -> None:
        return None

    def snapshot(self) -> dict:
        return {"provider": self.name}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


# ------------------------------------------------------------------- chain

async def test_falls_to_next_entry_in_same_call():
    a = FakeProvider("cloud_a", exc=ProviderRateLimited("429"))
    b = FakeProvider("cloud_b")
    local = FakeProvider("local_whisper")
    chain = SttFailoverChain([
        ChainEntry(a, cooldown_s=60),
        ChainEntry(b, cooldown_s=60),
        ChainEntry(local, cooldown_s=0),
    ])

    result = await chain.transcribe(_req(), fence=None)
    assert result.provider == "cloud_b"
    assert a.calls == 1 and b.calls == 1 and local.calls == 0


async def test_failed_entry_skipped_during_cooldown_then_retried():
    clock = FakeClock()
    a = FakeProvider("cloud_a", exc=ProviderUnavailable("503"))
    b = FakeProvider("cloud_b")
    local = FakeProvider("local_whisper")
    chain = SttFailoverChain(
        [ChainEntry(a, 60), ChainEntry(b, 60), ChainEntry(local, 0)], clock=clock
    )

    await chain.transcribe(_req(), fence=None)  # a падает, b отвечает
    assert a.calls == 1

    await chain.transcribe(_req(), fence=None)  # a на cooldown — пропущен
    assert a.calls == 1
    assert b.calls == 2

    clock.advance(61)
    await chain.transcribe(_req(), fence=None)  # cooldown истёк — a пробуется снова
    assert a.calls == 2


async def test_response_invalid_does_not_cooldown_but_moves_on():
    a = FakeProvider("cloud_a", exc=ProviderResponseInvalid("400"))
    b = FakeProvider("cloud_b")
    local = FakeProvider("local_whisper")
    chain = SttFailoverChain([ChainEntry(a, 60), ChainEntry(b, 60), ChainEntry(local, 0)])

    r1 = await chain.transcribe(_req(), fence=None)
    r2 = await chain.transcribe(_req(), fence=None)
    assert r1.provider == "cloud_b" and r2.provider == "cloud_b"
    # Битый ответ — не про доступность: звено не на cooldown, вызывается снова.
    assert a.calls == 2


async def test_all_cloud_blocked_goes_local_without_delay():
    clock = FakeClock()
    a = FakeProvider("cloud_a", exc=ProviderUnavailable("503"))
    b = FakeProvider("cloud_b", exc=ProviderUnavailable("503"))
    local = FakeProvider("local_whisper")
    chain = SttFailoverChain(
        [ChainEntry(a, 60), ChainEntry(b, 60), ChainEntry(local, 0)], clock=clock
    )

    await chain.transcribe(_req(), fence=None)  # оба облачных падают и уходят в cooldown
    local.calls = 0
    result = await chain.transcribe(_req(), fence=None)
    assert result.provider == "local_whisper"
    assert a.calls == 1 and b.calls == 1  # без повторных сетевых проб
    assert local.calls == 1


async def test_local_entry_must_have_no_cooldown():
    local = FakeProvider("local_whisper")
    with pytest.raises(ValueError):
        SttFailoverChain([ChainEntry(local, cooldown_s=60)])


async def test_exhausted_when_all_entries_fail():
    a = FakeProvider("cloud_a", exc=ProviderAuthError("401"))
    local = FakeProvider("local_whisper", exc=ProviderUnavailable("boom"))
    chain = SttFailoverChain([ChainEntry(a, 60), ChainEntry(local, 0)])
    with pytest.raises(SttChainExhausted):
        await chain.transcribe(_req(), fence=None)


async def test_not_implemented_cloud_falls_back_to_local():
    """Облачный STT ещё не реализован (D2/D3) — цепочка обязана дойти до local."""
    a = FakeProvider("cloud_a", exc=NotImplementedError("cloud STT not implemented"))
    local = FakeProvider("local_whisper")
    chain = SttFailoverChain([ChainEntry(a, 60), ChainEntry(local, 0)])
    result = await chain.transcribe(_req(), fence=None)
    assert result.provider == "local_whisper"


# ----------------------------------------------------------------- factory

def test_build_stt_chain_from_section():
    stt = SttSection(
        mode="file_per_segment",
        json_output=True,
        language_autodetect=True,
        chain=(
            SttChainEntry("openai_api", "whisper-1", key_name="stt_openai", cooldown_s=60),
            SttChainEntry(
                "local_whisper", "ggml-base.bin",
                fallback_model="ggml-tiny.bin", cooldown_s=0,
            ),
        ),
    )
    chain = build_stt_chain(stt)
    names = [e.provider.name for e in chain._entries]  # noqa: SLF001
    assert names == ["openai_api", "local_whisper"]
    assert chain._entries[-1].cooldown_s == 0  # noqa: SLF001


def test_default_chain_is_local_only():
    chain = build_stt_chain(default_stt_section())
    names = [e.provider.name for e in chain._entries]  # noqa: SLF001
    assert names == ["local_whisper"]


def test_resolve_model_path():
    assert resolve_model_path("ggml-base.bin") == Path("models/ggml-base.bin")
    assert resolve_model_path("/abs/base.bin") == Path("/abs/base.bin")
    assert resolve_model_path("custom/dir/m.bin") == Path("custom/dir/m.bin")


# ---------------------------------------------------------------- облако

def _cloud(privacy=None, key_provider=None) -> CloudSttProvider:
    return CloudSttProvider(
        active="openai_api",
        endpoint="",
        model="whisper-1",
        privacy=privacy,
        key_provider=key_provider,
    )


async def test_cloud_privacy_blocks_confidential():
    p = _cloud(privacy=PrivacyController(PrivacyProfile.CONFIDENTIAL))
    with pytest.raises(Exception) as exc_info:
        await p.transcribe(_req())
    assert type(exc_info.value).__name__ == "PrivacyViolation"


async def test_cloud_requires_key():
    def no_key() -> str:
        raise ProviderAuthError("unknown provider: stt_cloud")

    p = _cloud(privacy=PrivacyController(PrivacyProfile.OPEN), key_provider=no_key)
    with pytest.raises(ProviderAuthError):
        await p.transcribe(_req())
