"""tests/test_privacy_isolation.py — изоляция профилей. Задача J4.

Критерий приёмки 7: при переключении в конфиденциальный профиль исходящий
аудиотрафик прекращается в течение 500 мс; в конфиденциальном профиле
сетевой трафик с аудио отсутствует.

Метод: транспорт realtime-сессии подменяется перехватчиком, который
записывает каждый send(). Тест утверждает свойства ПОТОКА БАЙТОВ, а не
внутреннего состояния объектов — именно так формулирует приёмку спека
(«проверяется перехватом»).

На живом железе тот же контракт проверяется nftables-счётчиком; этот файл —
CI-уровень той же гарантии.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from app.privacy import (
    Capability,
    Fence,
    PrivacyController,
    PrivacyProfile,
)
from app.errors import PrivacyViolation, StaleGenerationError
from app.translation.providers.openai_realtime import (
    OpenAIRealtimeSession,
    RealtimeConfig,
)


class WireTap:
    """Перехватывающий транспорт: фиксирует всё, что уходит «в сеть»."""

    def __init__(self) -> None:
        self.sent: list[tuple[float, dict]] = []
        self.closed = False
        self.aborted = False
        self._recv_q: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, data: str) -> None:
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append((time.monotonic(), json.loads(data)))

    async def recv(self) -> str:
        return await self._recv_q.get()

    async def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True
        self.aborted = True

    # --- утверждения о потоке байтов ---

    def audio_frames(self) -> list[tuple[float, bytes]]:
        return [
            (ts, base64.b64decode(msg["audio"]))
            for ts, msg in self.sent
            if msg.get("type") == "input_audio_buffer.append"
        ]

    def audio_after(self, t: float) -> list[bytes]:
        return [pcm for ts, pcm in self.audio_frames() if ts > t]


@pytest.fixture
def privacy() -> PrivacyController:
    return PrivacyController(PrivacyProfile.OPEN)


def make_session(privacy: PrivacyController, tap: WireTap) -> OpenAIRealtimeSession:
    async def factory(url: str, headers: dict[str, str]) -> WireTap:
        # Ключ обязан быть в заголовке и нигде больше.
        assert headers["Authorization"].startswith("Bearer ")
        return tap

    async def sink(delta) -> None:  # noqa: ANN001
        pass

    return OpenAIRealtimeSession(
        RealtimeConfig(),
        privacy,
        key_provider=lambda: "sk-test-XYZ",
        on_delta=sink,
        transport_factory=factory,
    )


# ================================================================== сценарии

@pytest.mark.asyncio
async def test_open_profile_sends_audio(privacy: PrivacyController) -> None:
    tap = WireTap()
    session = make_session(privacy, tap)
    await session.open()
    await session.push(b"\x01\x02" * 160)
    await asyncio.sleep(0.05)
    await session.close()

    frames = tap.audio_frames()
    assert len(frames) == 1
    assert frames[0][1] == b"\x01\x02" * 160


@pytest.mark.asyncio
async def test_confidential_profile_never_opens(privacy: PrivacyController) -> None:
    """В закрытом профиле сессия не открывается вовсе: ноль байтов."""
    await privacy.switch(PrivacyProfile.CONFIDENTIAL)
    tap = WireTap()
    session = make_session(privacy, tap)
    with pytest.raises(PrivacyViolation):
        await session.open()
    assert tap.sent == []


@pytest.mark.asyncio
async def test_switch_stops_audio_within_deadline(
    privacy: PrivacyController,
) -> None:
    """Ядро критерия 7: после switch() — ни одного аудиокадра в сокете,
    teardown укладывается в 500 мс."""
    tap = WireTap()
    session = make_session(privacy, tap)
    await session.open()

    # Поток аудио идёт непрерывно в фоне, как из _accept_partial.
    stop_pushing = asyncio.Event()

    async def pusher() -> None:
        while not stop_pushing.is_set():
            try:
                await session.push(b"\x00" * 320)
            except StaleGenerationError:
                return
            await asyncio.sleep(0.005)

    push_task = asyncio.create_task(pusher())
    await asyncio.sleep(0.05)
    assert tap.audio_frames(), "аудио не пошло до переключения — тест слеп"

    switch_at = time.monotonic()
    teardown_ms = await privacy.switch(PrivacyProfile.CONFIDENTIAL)
    stop_pushing.set()
    await push_task
    await asyncio.sleep(0.1)  # даём фоновым задачам шанс нарушить запрет

    leaked = tap.audio_after(switch_at)
    assert leaked == [], f"утечка аудио после переключения: {len(leaked)} кадров"
    assert teardown_ms <= 500, f"teardown {teardown_ms} мс > дедлайна 500 мс"
    assert tap.closed or tap.aborted, "транспорт не закрыт"


@pytest.mark.asyncio
async def test_stale_fence_rejected_before_socket(
    privacy: PrivacyController,
) -> None:
    """Гонка TOCTOU: fence захвачен до переключения — результат отброшен,
    в сокет не попадает ничего."""
    tap = WireTap()
    session = make_session(privacy, tap)
    await session.open()
    baseline = len(tap.audio_frames())

    await privacy.switch(PrivacyProfile.CONFIDENTIAL)
    with pytest.raises(StaleGenerationError):
        await session.push(b"\x00" * 320)
    await asyncio.sleep(0.05)
    assert len(tap.audio_frames()) == baseline


@pytest.mark.asyncio
async def test_validate_rejects_inflight_result(privacy: PrivacyController) -> None:
    """Результат, «прилетевший» под старым поколением, не проходит validate."""
    fence: Fence = privacy.require(Capability.AUDIO_TO_CLOUD)
    await privacy.switch(PrivacyProfile.CONFIDENTIAL)
    with pytest.raises(StaleGenerationError):
        privacy.validate(fence, Capability.AUDIO_TO_CLOUD)


@pytest.mark.asyncio
async def test_text_allowed_in_confidential(privacy: PrivacyController) -> None:
    """Матрица A6: текст и локальный STT в закрытом профиле разрешены."""
    await privacy.switch(PrivacyProfile.CONFIDENTIAL)
    assert privacy.allows(Capability.TEXT_TO_CLOUD)
    assert privacy.allows(Capability.LOCAL_STT)
    assert privacy.allows(Capability.DRAFT_GENERATION)
    assert not privacy.allows(Capability.AUDIO_TO_CLOUD)


@pytest.mark.asyncio
async def test_reopen_after_return_to_open(privacy: PrivacyController) -> None:
    """Возврат в открытый профиль: новая сессия работает под новым поколением."""
    tap1 = WireTap()
    s1 = make_session(privacy, tap1)
    await s1.open()
    await privacy.switch(PrivacyProfile.CONFIDENTIAL)
    await privacy.switch(PrivacyProfile.OPEN)

    tap2 = WireTap()
    s2 = make_session(privacy, tap2)
    await s2.open()
    await s2.push(b"\x07" * 320)
    await asyncio.sleep(0.05)
    await s2.close()
    assert len(tap2.audio_frames()) == 1
