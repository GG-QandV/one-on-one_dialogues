"""tests/test_stt_provider.py — STT-провайдерная архитектура (REFACTOR_STT).

Покрывает: обратную совместимость конфига, валидацию, сериализацию,
фабрику провайдеров, приватностный гейт/ключ облака, проводку scheduler
и эндпоинты GET/POST /api/stt.
"""

from __future__ import annotations

import asyncio
import dataclasses
import socket
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

from app.audio.segmenter import FinalSegment
from app.config import (
    FLAT_DEFAULTS,
    STT_ACTIVE_CHOICES,
    SttCloudSection,
    SttLocalSection,
    SttSection,
    default_stt_section,
    load,
    to_toml,
    validate,
)
from app.errors import PrivacyViolation, ProviderAuthError
from app.privacy import PrivacyController, PrivacyProfile
from app.security.byok import KeyStore
from app.stt.base import SttRequest, SttResult
from app.stt.cloud_api import CloudSttProvider
from app.stt.factory import build_stt_provider, resolve_model_path
from app.stt.local_whisper import LocalWhisperProvider
from app.stt.scheduler import SchedulerConfig, SttScheduler
from app.ui.server import UiConfig, UiServer

OLD_STT_TOML = """
[privacy]
default_profile = "open"

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = true

[ui]
host = "127.0.0.1"
port = 8790
"""


# ----------------------------------------------------------------- конфиг

def test_old_config_without_nested_stt_loads(tmp_path):
    """Старый config.toml без [stt.local]/[stt.cloud] грузится; active дефолтится."""
    p = tmp_path / "config.toml"
    p.write_text(OLD_STT_TOML, encoding="utf-8")
    cfg = load(p)
    assert cfg.stt.active == "local_whisper"
    assert cfg.stt.local.model == "ggml-base.bin"
    assert cfg.stt.local.device == "auto"


def test_cloud_active_requires_model():
    flat = dict(FLAT_DEFAULTS)
    flat["stt.active"] = "openai_api"
    flat["stt.cloud.model"] = ""
    errors = validate(flat)
    assert any("stt.cloud.model must be set" in e for e in errors)


def test_active_choice_validated():
    flat = dict(FLAT_DEFAULTS)
    flat["stt.active"] = "nonsense"
    errors = validate(flat)
    assert any("stt.active must be one of" in e for e in errors)
    assert STT_ACTIVE_CHOICES == ["local_whisper", "openai_api", "custom_api"]


def test_to_toml_serializes_nested_stt_sections():
    text = to_toml(_config_with(default_stt_section()))
    assert "[stt]" in text
    assert "[stt.local]" in text
    assert "[stt.cloud]" in text


def _config_with(stt: SttSection):
    """Собрать минимальный Config с заданной секцией stt (для to_toml)."""
    from app.config import _dict_to_config

    cfg = _dict_to_config(FLAT_DEFAULTS)
    return dataclasses.replace(cfg, stt=stt)


# ---------------------------------------------------------------- фабрика

def test_factory_builds_local_provider():
    p = build_stt_provider(default_stt_section())
    assert isinstance(p, LocalWhisperProvider)
    assert p.name == "local_whisper"


def test_factory_builds_cloud_provider():
    stt = SttSection(
        active="openai_api",
        mode="file_per_segment",
        json_output=True,
        language_autodetect=True,
        local=SttLocalSection("ggml-base.bin", "ggml-tiny.bin", "auto"),
        cloud=SttCloudSection("", "whisper-1", "", 15.0),
    )
    p = build_stt_provider(stt)
    assert isinstance(p, CloudSttProvider)
    assert p.name == "openai_api"


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


@pytest.mark.asyncio
async def test_cloud_privacy_blocks_confidential():
    p = _cloud(privacy=PrivacyController(PrivacyProfile.CONFIDENTIAL))
    with pytest.raises(PrivacyViolation):
        await p.transcribe(SttRequest(Path("/dev/null"), "s1"))


@pytest.mark.asyncio
async def test_cloud_requires_key():
    def no_key() -> str:
        raise ProviderAuthError("unknown provider: stt_cloud")

    p = _cloud(privacy=PrivacyController(PrivacyProfile.OPEN), key_provider=no_key)
    with pytest.raises(ProviderAuthError):
        await p.transcribe(SttRequest(Path("/dev/null"), "s1"))


@pytest.mark.asyncio
async def test_cloud_call_not_implemented_yet():
    p = _cloud(
        privacy=PrivacyController(PrivacyProfile.OPEN),
        key_provider=lambda: "sk-test",
    )
    with pytest.raises(NotImplementedError):
        await p.transcribe(SttRequest(Path("/dev/null"), "s1"))
    # Ключ в snapshot не попадает.
    assert "sk-test" not in str(p.snapshot())


# -------------------------------------------------------------- scheduler

class _FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.seen: list[SttRequest] = []

    async def transcribe(self, req: SttRequest, *, fence=None) -> SttResult:
        self.seen.append(req)
        return SttResult(raw_text="hello", model="fake-model")

    async def close(self) -> None:
        return None

    def snapshot(self) -> dict:
        return {"provider": self.name, "last_rtf": None}


@pytest.mark.asyncio
async def test_scheduler_routes_through_provider():
    provider = _FakeProvider()
    results: list[SttResult] = []

    async def on_result(seg, res):
        results.append(res)

    async def on_error(seg, exc):
        raise AssertionError(f"unexpected error: {exc}")

    sched = SttScheduler(
        provider, on_result=on_result, on_error=on_error, config=SchedulerConfig()
    )
    seg = FinalSegment(
        id="s1", role="microphone", t_start_ms=0, t_end_ms=1000,
        audio_path=Path("/dev/null"), reason=None, mean_level_db=0.0,  # type: ignore[arg-type]
    )
    assert sched.submit(seg) is True
    await sched.start()
    for _ in range(50):
        if results:
            break
        await asyncio.sleep(0.01)
    await sched.stop()

    assert results and results[0].raw_text == "hello"
    assert provider.seen[0].segment_id == "s1"


@pytest.mark.asyncio
async def test_scheduler_set_provider_swaps():
    a, b = _FakeProvider(), _FakeProvider()
    sched = SttScheduler(
        a, on_result=_noop, on_error=_err, config=SchedulerConfig()
    )
    old = sched.set_provider(b)
    assert old is a
    assert sched._provider is b  # noqa: SLF001 — проверяем сам факт подмены


async def _noop(seg, res):  # pragma: no cover - helper
    return None


async def _err(seg, exc):  # pragma: no cover - helper
    raise AssertionError(exc)


# ------------------------------------------------------------- HTTP /api/stt

class _FakeApp:
    def __init__(self, stt: SttSection, keystore: KeyStore) -> None:
        self._stt = stt
        self.keystore = keystore
        self.reloaded: SttSection | None = None

    @property
    def stt_config(self) -> SttSection:
        return self._stt

    async def update_config(self, changes: dict):
        stt_changes = changes.get("stt", {})
        self._stt = dataclasses.replace(
            self._stt,
            active=stt_changes.get("active", self._stt.active),
        )
        return SimpleNamespace(stt=self._stt)

    async def reload_stt_provider(self, stt_cfg: SttSection) -> None:
        self.reloaded = stt_cfg


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_stt_endpoints_get_and_post():
    port = _free_port()
    keystore = KeyStore()
    fake = _FakeApp(default_stt_section(), keystore)
    server = UiServer(fake, UiConfig(host="127.0.0.1", port=port, heartbeat_s=0.1))
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/api/stt") as resp:
                assert resp.status == 200
                body = await resp.json()
                assert body["active"] == "local_whisper"
                assert body["choices"] == STT_ACTIVE_CHOICES
                assert body["cloud"]["key_present"] is False
                assert "key" not in body

            # Ключ через тот же /api/key (KeyStore), не в TOML
            async with session.post(
                f"http://127.0.0.1:{port}/api/key",
                json={"provider": "stt_cloud", "key": "sk-abcdef12345"},
            ) as resp:
                assert resp.status == 200
                assert "sk-abcdef" not in (await resp.text())

            async with session.get(f"http://127.0.0.1:{port}/api/stt") as resp:
                body = await resp.json()
                assert body["cloud"]["key_present"] is True
                assert body["cloud"]["key_masked"] == "sk-...2345"

            async with session.post(
                f"http://127.0.0.1:{port}/api/stt",
                json={"active": "openai_api", "cloud": {"model": "whisper-1"}},
            ) as resp:
                assert resp.status == 204
            assert fake.reloaded is not None
            assert fake.reloaded.active == "openai_api"

            async with session.post(
                f"http://127.0.0.1:{port}/api/stt",
                json={"active": "bogus"},
            ) as resp:
                assert resp.status == 400
    finally:
        await server.stop()
