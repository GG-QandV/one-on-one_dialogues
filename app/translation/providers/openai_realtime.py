"""app/translation/providers/openai_realtime.py — быстрый трек. Задача D5.

Спека: раздел 9 (gpt-realtime-translate: только входящий поток, только
открытый профиль), раздел 10.2 (интерфейс RealtimeProvider), раздел 2
(закрытие сессии при переключении профиля за 500 мс).

Архитектура
-----------
Транспорт (WebSocket) отделён от логики сессии интерфейсом ``Transport``.
Причины две:
  * тест изоляции профилей (J4) обязан перехватывать каждый исходящий байт —
    с инжектируемым транспортом это делается без monkey-патчей и сети;
  * библиотека websockets не должна протекать в сигнатуры: её замена
    (например, на aiohttp) не тронет ни privacy, ни main.

Fencing
-------
Каждый push аудио проверяет fence, захваченный при открытии сессии.
Переключение профиля инвалидирует fence синхронно (app/privacy.py), поэтому
между «профиль стал confidential» и «данные ушли в сокет» нет окна: push
падает StaleGenerationError раньше записи в транспорт. Закрытие самого
сокета — второй, страховочный механизм через TeardownHook.

Реконнект
---------
Долгая сессия (1-2 часа, риск R14) рвётся. Реконнект восстанавливает
соединение с экспоненциальной паузой, но НЕ доотправляет аудио, накопленное
за время разрыва: быстрый трек — черновик, его пропуск ничего не теряет
(точный трек локален и не зависит от этого модуля). Доотправка старого аудио
дала бы черновики с опозданием в секунды — хуже, чем их отсутствие.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from app.errors import (
    ProviderAuthError,
    ProviderResponseInvalid,
    ProviderUnavailable,
    StaleGenerationError,
)
from app.privacy import Capability, Fence, PrivacyController

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ транспорт

class Transport(Protocol):
    """Минимальный контракт WebSocket-соединения."""

    async def send(self, data: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...
    def abort(self) -> None:
        """Синхронный обрыв без рукопожатия закрытия."""
        ...


TransportFactory = Callable[[str, dict[str, str]], Awaitable[Transport]]


async def default_transport_factory(url: str, headers: dict[str, str]) -> Transport:
    """Боевой транспорт на websockets. Импорт внутри: тестам и smoke
    библиотека не нужна, а её отсутствие не должно ломать импорт модуля."""
    import websockets  # noqa: PLC0415

    ws = await websockets.connect(url, additional_headers=headers, max_size=1 << 20)

    class _Ws:
        async def send(self, data: str) -> None:
            await ws.send(data)

        async def recv(self) -> str:
            return await ws.recv()

        async def close(self) -> None:
            await ws.close()

        def abort(self) -> None:
            # transport-level close без CLOSE-фрейма: жёсткий обрыв.
            with contextlib.suppress(Exception):
                ws.transport.close()  # type: ignore[attr-defined]

    return _Ws()


# ------------------------------------------------------------------- события

class DeltaKind(str, Enum):
    PARTIAL = "partial"      # промежуточный текст перевода
    COMPLETED = "completed"  # финальный текст фрагмента


@dataclass(frozen=True, slots=True)
class TranscriptDelta:
    kind: DeltaKind
    text: str
    utterance_hint: str | None
    received_at_ms: int


DeltaSink = Callable[[TranscriptDelta], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RealtimeConfig:
    url: str = "wss://api.openai.com/v1/realtime?model=gpt-realtime-translate"
    source_language: str = "en"
    target_language: str = "ru"
    reconnect_base_s: float = 0.5
    reconnect_max_s: float = 10.0
    #: После стольких подряд неудачных реконнектов сессия сдаётся
    #: (быстрый трек гаснет, точный работает — деградация, не авария).
    reconnect_give_up: int = 6
    send_queue_max: int = 50


@dataclass
class RealtimeStats:
    state: str = "idle"
    chunks_sent: int = 0
    bytes_sent: int = 0
    deltas_received: int = 0
    reconnects: int = 0
    dropped_stale: int = 0
    dropped_backlog: int = 0
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return dict(self.__dict__)


class OpenAIRealtimeSession:
    """Одна облачная сессия быстрого трека. Реализует TeardownHook.

    Использование::

        session = OpenAIRealtimeSession(cfg, privacy, key_provider, on_delta)
        await session.open()                # регистрирует себя в privacy
        await session.push(pcm_bytes)       # из _accept_partial (main)
        ...
        await session.close()

    Ключ приходит через key_provider, а не хранится в объекте: BYOK-модуль
    (G2) владеет временем жизни ключа, сессия лишь запрашивает его в момент
    подключения. В логи и исключения ключ не попадает (SecretFreeError).
    """

    name = "openai-realtime"

    def __init__(
        self,
        config: RealtimeConfig,
        privacy: PrivacyController,
        key_provider: Callable[[], str],
        on_delta: DeltaSink,
        *,
        transport_factory: TransportFactory = default_transport_factory,
    ) -> None:
        self._cfg = config
        self.privacy = privacy
        self._key_provider = key_provider
        self._on_delta = on_delta
        self._factory = transport_factory

        self._transport: Transport | None = None
        self._fence: Fence | None = None
        self._send_q: asyncio.Queue[bytes] = asyncio.Queue(config.send_queue_max)
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = asyncio.Event()
        self.stats = RealtimeStats()

    # -------------------------------------------------------------- открытие

    async def open(self) -> None:
        # Право и fence одним действием: TOCTOU-окна нет.
        self._fence = self.privacy.require(Capability.AUDIO_TO_CLOUD)
        await self._connect()
        self.privacy.register_hook(self)
        self._tasks = [
            asyncio.create_task(self._sender(), name="rt-sender"),
            asyncio.create_task(self._receiver(), name="rt-receiver"),
        ]
        self.stats.state = "open"
        log.info("realtime-сессия открыта (%s)", self._fence)

    async def _connect(self) -> None:
        key = self._key_provider()
        if not key:
            raise ProviderAuthError("BYOK-ключ отсутствует")
        headers = {"Authorization": f"Bearer {key}"}
        try:
            self._transport = await self._factory(self._cfg.url, headers)
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(f"подключение не удалось: {type(exc).__name__}") from exc
        await self._transport.send(json.dumps({
            "type": "session.update",
            "session": {
                "input_audio_format": "pcm16",
                "source_language": self._cfg.source_language,
                "target_language": self._cfg.target_language,
            },
        }))

    # ------------------------------------------------------------------ push

    async def push(self, pcm: bytes) -> None:
        """Отдать кусок аудио в отправку. Неблокирующий по замыслу.

        Порядок проверок жёсткий: сначала fence (приватность), потом всё
        остальное. Переполнение очереди отправки — сброс куска, не ожидание:
        ждать здесь — значит тормозить конвейер захвата ради черновика.
        """
        if self._fence is None or self.privacy.is_stale(self._fence):
            self.stats.dropped_stale += 1
            raise StaleGenerationError("push после переключения профиля")
        if self._closed.is_set():
            return
        try:
            self._send_q.put_nowait(pcm)
        except asyncio.QueueFull:
            self.stats.dropped_backlog += 1

    async def _sender(self) -> None:
        while not self._closed.is_set():
            pcm = await self._send_q.get()
            # Повторная проверка fence непосредственно перед записью в сокет:
            # между put и get профиль мог переключиться.
            if self._fence is None or self.privacy.is_stale(self._fence):
                self.stats.dropped_stale += 1
                continue
            if self._transport is None:
                continue
            try:
                await self._transport.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode("ascii"),
                }))
                self.stats.chunks_sent += 1
                self.stats.bytes_sent += len(pcm)
            except Exception:  # noqa: BLE001
                await self._handle_disconnect("send")

    # --------------------------------------------------------------- receiver

    async def _receiver(self) -> None:
        while not self._closed.is_set():
            if self._transport is None:
                await asyncio.sleep(0.05)
                continue
            try:
                raw = await self._transport.recv()
            except Exception:  # noqa: BLE001
                await self._handle_disconnect("recv")
                continue
            delta = self._parse(raw)
            if delta is None:
                continue
            self.stats.deltas_received += 1
            try:
                await self._on_delta(delta)
            except Exception:  # noqa: BLE001
                log.exception("обработчик дельты упал")

    def _parse(self, raw: str) -> TranscriptDelta | None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderResponseInvalid(f"невалидный кадр: {exc}") from exc
        mtype = msg.get("type", "")
        now = int(time.monotonic() * 1000)
        if mtype.endswith("transcript.delta"):
            return TranscriptDelta(DeltaKind.PARTIAL, msg.get("delta", ""),
                                   msg.get("item_id"), now)
        if mtype.endswith("transcript.done"):
            return TranscriptDelta(DeltaKind.COMPLETED, msg.get("transcript", ""),
                                   msg.get("item_id"), now)
        if mtype == "error":
            code = msg.get("error", {}).get("code", "unknown")
            log.error("realtime-ошибка провайдера: %s", code)
            self.stats.last_error = code
        return None

    # -------------------------------------------------------------- реконнект

    async def _handle_disconnect(self, where: str) -> None:
        if self._closed.is_set():
            return
        self.stats.state = "reconnecting"
        log.warning("realtime-соединение потеряно (%s), реконнект", where)
        if self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.abort()
            self._transport = None

        # Аудио, накопленное за разрыв, выбрасывается осознанно (см. докстринг).
        while not self._send_q.empty():
            self._send_q.get_nowait()
            self.stats.dropped_backlog += 1

        delay = self._cfg.reconnect_base_s
        for attempt in range(1, self._cfg.reconnect_give_up + 1):
            if self._closed.is_set():
                return
            # Fence мог протухнуть, пока лежали в паузе — не реконнектимся
            # в профиль, который аудио запрещает.
            if self._fence is None or self.privacy.is_stale(self._fence):
                log.info("реконнект отменён: профиль переключён")
                await self.close()
                return
            await asyncio.sleep(delay)
            try:
                await self._connect()
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = type(exc).__name__
                delay = min(delay * 2, self._cfg.reconnect_max_s)
                continue
            self.stats.reconnects += 1
            self.stats.state = "open"
            log.info("realtime-соединение восстановлено (попытка %d)", attempt)
            return

        log.error("realtime: реконнект исчерпан, быстрый трек отключён")
        self.stats.state = "given_up"
        await self.close()

    # ------------------------------------------------------------ TeardownHook

    async def teardown(self) -> None:
        """Штатное закрытие по переключению профиля (дедлайн у вызывающего)."""
        await self.close()

    def force_close(self) -> None:
        """Аварийное закрытие при просроченном дедлайне: без рукопожатий."""
        self._closed.set()
        if self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.abort()
            self._transport = None
        self.stats.state = "closed"

    async def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self.stats.state = "closing"
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self._transport is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._transport.close(), timeout=0.3)
            self._transport = None
        self.privacy.unregister_hook(self.name)
        self.stats.state = "closed"
        log.info("realtime-сессия закрыта: %s", self.stats.snapshot())
