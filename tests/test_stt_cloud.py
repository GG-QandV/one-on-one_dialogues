"""tests/test_stt_cloud.py — облачный STT (Groq/OpenAI-совместимый) и файловые ключи.

Покрывает: `_parse` (verbose_json), реальный multipart-вызов против локального
HTTP-сервера, классификацию 401, чтение секретов ~/.secrets и файловый fallback
фабрики цепочки.
"""

from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest
from aiohttp import web

from app.config import SttChainEntry, SttSection
from app.errors import ProviderAuthError, ProviderResponseInvalid
from app.privacy import PrivacyController, PrivacyProfile
from app.security.byok import KeyStore
from app.security.keyfiles import load_key_file, read_secret_file
from app.stt.base import SttRequest
from app.stt.cloud_api import CloudSttProvider
from app.stt.factory import build_stt_cloud_chain


def _req(lang: str | None = None) -> SttRequest:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.write(fd, b"RIFF0000WAVEfmt ")
    os.close(fd)
    return SttRequest(audio_path=Path(path), segment_id="s1", language_hint=lang)


# ---------------------------------------------------------------- _parse

def test_parse_verbose_json():
    p = CloudSttProvider(active="custom_api", endpoint="", model="whisper-large-v3-turbo")
    payload = (
        '{"text": " hello ", "language": "en", '
        '"segments": [{"avg_logprob": -0.2}, {"avg_logprob": -0.4}]}'
    )
    res = p._parse(_req(), payload)  # noqa: SLF001
    assert res.raw_text == "hello"
    assert res.detected_language == "en"
    assert res.confidence == pytest.approx(-0.3)
    assert res.model == "whisper-large-v3-turbo"


def test_parse_missing_text_raises():
    p = CloudSttProvider(active="custom_api", endpoint="", model="m")
    with pytest.raises(ProviderResponseInvalid):
        p._parse(_req(), '{"language": "en"}')  # noqa: SLF001


def test_parse_invalid_json_raises():
    p = CloudSttProvider(active="custom_api", endpoint="", model="m")
    with pytest.raises(ProviderResponseInvalid):
        p._parse(_req(), "not-json")  # noqa: SLF001


# ------------------------------------------------------------ HTTP _call

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def stt_server():
    """Локальный OpenAI-совместимый endpoint; пишет разобранный запрос в seen."""
    seen: dict = {}

    async def handler(request: web.Request) -> web.Response:
        seen["auth"] = request.headers.get("Authorization")
        reader = await request.multipart()
        fields: dict = {}
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                fields["file_bytes"] = len(await part.read())
            else:
                fields[part.name] = (await part.text()).strip()
        seen["fields"] = fields
        if fields.get("model") == "unauthorized":
            return web.json_response({"error": "invalid api key"}, status=401)
        return web.json_response(
            {
                "text": "hello groq",
                "language": "en",
                "segments": [{"avg_logprob": -0.1}],
            }
        )

    app = web.Application()
    app.router.add_post("/transcribe", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        yield f"http://127.0.0.1:{port}/transcribe", seen
    finally:
        await runner.cleanup()


async def test_call_multipart_and_parse_roundtrip(stt_server):
    url, seen = stt_server
    p = CloudSttProvider(
        active="custom_api",
        endpoint=url,
        model="whisper-large-v3-turbo",
        privacy=PrivacyController(PrivacyProfile.OPEN),
        key_provider=lambda: "groq-secret",
    )
    res = await p.transcribe(_req(lang="ru"))

    assert res.raw_text == "hello groq"
    assert res.detected_language == "en"
    assert res.model == "whisper-large-v3-turbo"
    assert seen["auth"] == "Bearer groq-secret"
    assert seen["fields"]["model"] == "whisper-large-v3-turbo"
    assert seen["fields"]["language"] == "ru"
    assert seen["fields"]["response_format"] == "verbose_json"
    assert seen["fields"]["file_bytes"] > 0


async def test_call_401_classified_as_auth_error(stt_server):
    url, _ = stt_server
    p = CloudSttProvider(
        active="custom_api",
        endpoint=url,
        model="unauthorized",
        privacy=PrivacyController(PrivacyProfile.OPEN),
        key_provider=lambda: "bad",
    )
    with pytest.raises(ProviderAuthError):
        await p.transcribe(_req())


async def test_call_without_endpoint_for_custom_raises():
    from app.errors import ProviderUnavailable

    p = CloudSttProvider(
        active="custom_api",
        endpoint="",
        model="m",
        privacy=PrivacyController(PrivacyProfile.OPEN),
        key_provider=lambda: "k",
    )
    with pytest.raises(ProviderUnavailable):
        await p.transcribe(_req())


# ------------------------------------------------------------- key files

def test_read_secret_file(tmp_path: Path):
    (tmp_path / "groq_0").write_text("  sk-groq-0\n", encoding="utf-8")
    assert read_secret_file("groq_0", tmp_path) == "sk-groq-0"
    assert read_secret_file("missing", tmp_path) is None
    assert read_secret_file("", tmp_path) is None
    assert read_secret_file("../etc/passwd", tmp_path) is None
    assert read_secret_file(".hidden", tmp_path) is None


def test_load_key_file_into_keystore(tmp_path: Path):
    (tmp_path / "groq_0").write_text("sk-groq-0", encoding="utf-8")
    store = KeyStore()
    assert load_key_file(store, "groq_0", tmp_path) is True
    assert store.get("groq_0") == "sk-groq-0"
    assert load_key_file(store, "nope", tmp_path) is False


def test_factory_key_provider_falls_back_to_file(tmp_path: Path):
    (tmp_path / "groq_0").write_text("sk-from-file", encoding="utf-8")
    section = SttSection(
        mode="file_per_segment",
        json_output=True,
        language_autodetect=True,
        chain=(
            SttChainEntry(
                "custom_api", "whisper-large-v3-turbo", key_name="groq_0", cooldown_s=60
            ),
            SttChainEntry("local_whisper", "ggml-base.bin", cooldown_s=0),
        ),
    )
    store = KeyStore()
    chain = build_stt_cloud_chain(section, keystore=store, secrets_dir=tmp_path)
    assert chain is not None
    provider = chain._entries[0].provider  # noqa: SLF001
    assert provider.label == "custom_api:groq_0"
    assert provider._key_provider() == "sk-from-file"  # noqa: SLF001
    assert store.has("groq_0")
