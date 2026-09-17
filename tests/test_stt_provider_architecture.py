"""tests/test_stt_provider_architecture.py — контракт STT-провайдера (D1-зеркало).

Источник: `test_stt_provider_architecture.py` из TMP, адаптирован под реальный
харнесс проекта (asyncio_mode=auto; реальные фикстуры aiohttp-тестов вместо
заглушек; приватностный гейт проверяется через настоящий контракт
`PrivacyController.allows`, поэтому ожидаемое исключение — `PrivacyViolation`,
а не `ProviderAuthError`).

Группы: конфиг, контракт BaseSttProvider, KeyStore("stt_cloud"), HTTP /api/stt,
регресс остальных секций.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import aiohttp
import pytest

from app.ui.server import UiConfig, UiServer

# ============================================================ 1. Конфиг


class TestSttConfigDefaults:
    """Дефолты и обратная совместимость со старым config.toml."""

    def test_defaults_active_is_local_whisper(self):
        from app.config import defaults

        assert defaults()["stt"]["active"] == "local_whisper"

    def test_old_toml_without_stt_active_loads_with_default(self, tmp_path: Path):
        """Старый config.toml (без stt.active и [stt.local]/[stt.cloud]) грузится."""
        from app import config

        old_toml = """
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
        path = tmp_path / "config.toml"
        path.write_text(old_toml, encoding="utf-8")

        loaded = config.load(path)
        assert loaded.stt.active == "local_whisper"
        assert loaded.stt.local.model == "ggml-base.bin"


class TestSttConfigValidation:
    def test_active_openai_api_without_cloud_model_fails(self):
        from app.config import validate

        flat = _base_flat_config()
        flat["stt.active"] = "openai_api"
        flat["stt.cloud.model"] = ""
        assert any("stt.cloud.model" in e for e in validate(flat))

    def test_active_openai_api_with_cloud_model_passes(self):
        from app.config import validate

        flat = _base_flat_config()
        flat["stt.active"] = "openai_api"
        flat["stt.cloud.model"] = "whisper-1"
        assert not [e for e in validate(flat) if e.startswith("stt.")]

    def test_active_invalid_value_fails(self):
        from app.config import validate

        flat = _base_flat_config()
        flat["stt.active"] = "not_a_real_provider"
        assert any("stt.active" in e for e in validate(flat))

    def test_local_whisper_does_not_require_cloud_model(self):
        from app.config import validate

        flat = _base_flat_config()
        flat["stt.active"] = "local_whisper"
        flat["stt.cloud.model"] = ""
        assert not [e for e in validate(flat) if "stt.cloud.model" in e]

    def test_local_device_must_be_known_value(self):
        from app.config import validate

        flat = _base_flat_config()
        flat["stt.local.device"] = "quantum"
        assert any("stt.local.device" in e for e in validate(flat))


class TestSttConfigRoundtrip:
    def test_to_toml_then_load_preserves_stt_section(self, tmp_path: Path):
        from app import config

        cfg = config.load_or_default(tmp_path / "config.toml")
        toml_text = config.to_toml(cfg)
        assert "[stt.local]" in toml_text
        assert "[stt.cloud]" in toml_text

        path2 = tmp_path / "config2.toml"
        path2.write_text(toml_text, encoding="utf-8")
        reloaded = config.load(path2)
        assert reloaded.stt.active == cfg.stt.active
        assert reloaded.stt.local.model == cfg.stt.local.model

    def test_update_changes_only_stt_section(self, tmp_path: Path):
        from app import config

        path = tmp_path / "config.toml"
        config.load_or_default(path)

        updated = config.update(
            path, {"stt": {"active": "custom_api", "cloud": {"model": "my-model"}}}
        )
        assert updated.stt.active == "custom_api"
        assert updated.stt.cloud.model == "my-model"
        assert updated.privacy.default_profile == "open"


def _base_flat_config() -> dict:
    from app.config import FLAT_DEFAULTS

    return dict(FLAT_DEFAULTS)


# ============================================================ 2. Контракт


class FakePrivacy:
    """Подмена PrivacyController с реальным интерфейсом (allows/fence/validate)."""

    def __init__(self, allow: bool = True):
        self.allow = allow
        self.validated = False
        self.profile = self

    @property
    def value(self) -> str:
        return "open" if self.allow else "confidential"

    def allows(self, capability) -> bool:
        return self.allow

    def fence(self):
        return object()

    def validate(self, fence, capability) -> None:
        self.validated = True


def _make_provider(key: str, privacy):
    from app.stt.base import BaseSttProvider, SttResult

    class _P(BaseSttProvider):
        def __init__(self):
            super().__init__("test", privacy, key_provider=lambda: key)
            self._call_count = 0

        async def _call(self, req, api_key):
            self._call_count += 1
            return '{"text": "hello world", "language": "en"}'

        def _parse(self, req, raw):
            import json

            data = json.loads(raw)
            return SttResult(
                raw_text=data["text"],
                detected_language=data.get("language"),
                confidence=None,
            )

    return _P()


def _make_slow_provider(key: str, delay_s: float, timeout_s: float):
    from app.stt.base import BaseSttProvider, SttResult

    class _Slow(BaseSttProvider):
        def __init__(self):
            super().__init__(
                "slow", FakePrivacy(allow=True), key_provider=lambda: key,
                timeout_s=timeout_s,
            )

        async def _call(self, req, api_key):
            await asyncio.sleep(delay_s)
            return "{}"

        def _parse(self, req, raw):
            return SttResult(raw_text="", detected_language=None, confidence=None)

    return _Slow()


class TestSttProviderContract:
    """Критерии приёмки D1, применённые к SttProvider."""

    async def test_empty_key_raises_auth_error_without_network_call(self):
        from app.errors import ProviderAuthError
        from app.stt.base import SttRequest

        provider = _make_provider(key="", privacy=FakePrivacy(allow=True))
        req = SttRequest(audio_path=Path("/tmp/x.wav"), language_hint=None, segment_id="s1")

        with pytest.raises(ProviderAuthError):
            await provider.transcribe(req, fence=None)
        assert provider._call_count == 0

    async def test_confidential_profile_blocks_cloud_call(self):
        from app.errors import PrivacyViolation
        from app.stt.base import SttRequest

        provider = _make_provider(key="sk-test", privacy=FakePrivacy(allow=False))
        req = SttRequest(audio_path=Path("/tmp/x.wav"), language_hint=None, segment_id="s1")

        with pytest.raises(PrivacyViolation):
            await provider.transcribe(req, fence=None)
        assert provider._call_count == 0

    async def test_successful_call_returns_parsed_result(self):
        from app.stt.base import SttRequest

        provider = _make_provider(key="sk-test", privacy=FakePrivacy(allow=True))
        req = SttRequest(audio_path=Path("/tmp/x.wav"), language_hint=None, segment_id="s1")

        result = await provider.transcribe(req, fence=object())
        assert result.raw_text == "hello world"
        assert result.detected_language == "en"

    async def test_timeout_raises_provider_unavailable_without_leaking_audio_path(self):
        from app.errors import ProviderUnavailable
        from app.stt.base import SttRequest

        provider = _make_slow_provider(key="sk-test", delay_s=5.0, timeout_s=0.05)
        req = SttRequest(
            audio_path=Path("/tmp/секретный_путь.wav"), language_hint=None, segment_id="s1"
        )

        with pytest.raises(ProviderUnavailable) as exc_info:
            await provider.transcribe(req, fence=object())
        assert "секретный_путь" not in str(exc_info.value)

    @pytest.mark.parametrize(
        "status_code,expected_exc,retryable",
        [
            (401, "ProviderAuthError", False),
            (429, "ProviderRateLimited", True),
            (503, "ProviderUnavailable", True),
            (400, "ProviderResponseInvalid", False),
        ],
    )
    async def test_error_classification_matches_d1_table(
        self, status_code, expected_exc, retryable
    ):
        from app.stt.base import BaseSttProvider

        exc = BaseSttProvider._classify(status_code, body="irrelevant")
        assert type(exc).__name__ == expected_exc
        assert exc.retryable is retryable

    async def test_key_never_appears_in_repr_or_exceptions(self):
        from app.stt.base import SttRequest

        canary = "sk-CANARY-99999"
        provider = _make_provider(key=canary, privacy=FakePrivacy(allow=True))
        req = SttRequest(audio_path=Path("/tmp/x.wav"), language_hint=None, segment_id="s1")

        result = await provider.transcribe(req, fence=object())
        assert canary not in repr(provider)
        assert canary not in repr(result)


# ============================================================ 3. KeyStore


class TestSttKeystoreIntegration:
    """Переиспользование G2 KeyStore для нового провайдера 'stt_cloud'."""

    def test_put_get_roundtrip(self):
        from app.security.byok import KeyStore

        store = KeyStore()
        store.put("stt_cloud", "sk-CANARY-11111")
        assert store.get("stt_cloud") == "sk-CANARY-11111"

    def test_masked_hides_middle(self):
        from app.security.byok import KeyStore

        store = KeyStore()
        store.put("stt_cloud", "sk-CANARY-11111")
        assert "CANARY" not in store.masked("stt_cloud")

    def test_revoke_other_provider_unaffected(self):
        from app.security.byok import KeyStore

        store = KeyStore()
        store.put("stt_cloud", "sk-stt-key")
        store.put("gemini", "sk-gemini-key")
        store.revoke("stt_cloud")
        assert not store.has("stt_cloud")
        assert store.has("gemini")

    def test_key_not_in_repr_str_snapshot(self):
        from app.security.byok import KeyStore

        canary = "sk-CANARY-STT-77777"
        store = KeyStore()
        store.put("stt_cloud", canary)
        assert canary not in repr(store)
        assert canary not in str(store)
        assert canary not in str(store.snapshot())


# ============================================================ 4. HTTP /api/stt


class _Client:
    """Мини-клиент поверх aiohttp (близко к реальному паттерну test_ui_server)."""

    def __init__(self, base: str) -> None:
        self._base = base
        self._session: aiohttp.ClientSession | None = None

    async def _s(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get(self, path: str):
        return await (await self._s()).get(self._base + path)

    async def post(self, path: str, json=None):
        return await (await self._s()).post(self._base + path, json=json)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def stt_app(tmp_path: Path):
    from app.config import default_stt_section, load_or_default
    from app.main import AppConfig, Application
    from app.security.byok import KeyStore

    cfg_path = tmp_path / "config.toml"
    load_or_default(cfg_path)
    app = Application(AppConfig(stt=default_stt_section(), config_path=cfg_path))
    app.keystore = KeyStore()
    app._stt_cfg = default_stt_section()
    app._test_config_path = cfg_path
    return app


@pytest.fixture
def tmp_config_path(stt_app) -> Path:
    return stt_app._test_config_path


@pytest.fixture
async def app_under_test(stt_app):
    return stt_app


@pytest.fixture
async def ui_test_client(stt_app):
    port = _free_port()
    server = UiServer(stt_app, UiConfig(host="127.0.0.1", port=port, heartbeat_s=0.1))
    await server.start()
    client = _Client(f"http://127.0.0.1:{port}")
    try:
        yield client
    finally:
        await client.close()
        await server.stop()


class TestSttRoutes:
    async def test_get_stt_returns_active_and_choices_without_key(self, ui_test_client):
        resp = await ui_test_client.get("/api/stt")
        assert resp.status == 200
        body = await resp.json()
        assert body["active"] in ("local_whisper", "openai_api", "custom_api")
        assert "choices" in body
        assert "key" not in body.get("cloud", {})
        assert body["cloud"]["key_present"] is False

    async def test_post_stt_switches_active_provider(self, ui_test_client):
        resp = await ui_test_client.post(
            "/api/stt",
            json={
                "active": "openai_api",
                "cloud": {
                    "model": "whisper-1",
                    "endpoint": "",
                    "language_hint": "",
                    "timeout_s": 15,
                },
            },
        )
        assert resp.status == 204

        body = await (await ui_test_client.get("/api/stt")).json()
        assert body["active"] == "openai_api"
        assert body["cloud"]["model"] == "whisper-1"

    async def test_post_stt_invalid_active_returns_400(self, ui_test_client):
        resp = await ui_test_client.post("/api/stt", json={"active": "not_a_provider"})
        assert resp.status == 400

    async def test_post_stt_with_api_key_stores_in_keystore_not_config(
        self, ui_test_client, tmp_config_path
    ):
        resp = await ui_test_client.post(
            "/api/stt",
            json={
                "active": "openai_api",
                "cloud": {"model": "whisper-1"},
                "api_key": "sk-CANARY-ROUTE-42",
            },
        )
        assert resp.status == 204
        assert "sk-CANARY-ROUTE-42" not in tmp_config_path.read_text(encoding="utf-8")

    async def test_switch_without_restart_next_job_uses_new_provider(
        self, ui_test_client, app_under_test
    ):
        await ui_test_client.post(
            "/api/stt", json={"active": "openai_api", "cloud": {"model": "whisper-1"}}
        )
        assert app_under_test.stt_provider.name != "local_whisper"


# ============================================================ 5. Регресс


class TestSttRegression:
    def test_translation_and_draft_provider_sections_unaffected(self):
        from app.config import defaults

        cfg = defaults()
        assert cfg["provider"]["translation"]["active"] == "gemini"
        assert cfg["provider"]["draft"]["active"] == "gemini"
        assert cfg["provider"]["realtime"]["active"] == "openai"
