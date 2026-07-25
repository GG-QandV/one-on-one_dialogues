"""app/audio/discovery.py — обнаружение источников PipeWire. Задача C1.

Спека: раздел 18 «PipeWire».

Что здесь важно понимать
------------------------
В PipeWire «источник звука» — не одна сущность, а три разных случая:

  1. **Audio/Source** — физический вход: микрофон, линейный вход.
  2. **Монитор Audio/Sink** — то, что выводится на устройство. Захват монитора
     даёт весь системный звук целиком, включая уведомления и музыку.
  3. **Stream/Output/Audio** — поток конкретного приложения. Захват такого
     узла даёт звук только Zoom или только браузера.

Спека требует раздельный захват звука встречи и микрофона. Вариант 3 —
качественно лучше варианта 2: не тянет уведомления системы и чужие вкладки.
Но узлы приложений появляются только когда приложение играет звук и меняют id
при перезапуске. Поэтому:

  * узлы приложений сопоставляются по стабильному ключу
    ``application.name`` + ``media.name``, а не по числовому id;
  * при исчезновении узла capture-супервизор ждёт появления узла с тем же
    ключом, а не падает.

README обязан честно объяснить: универсального автозахвата нет, инструкция по
созданию virtual sink остаётся (спека, раздел 18). Этот модуль лишь даёт
список того, что реально существует в графе прямо сейчас.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.errors import AudioError, NodeNotFound

log = logging.getLogger(__name__)

_PW_DUMP_TIMEOUT_S = 5.0


class NodeKind(str, Enum):
    MICROPHONE = "microphone"          # Audio/Source
    SINK_MONITOR = "sink_monitor"      # монитор Audio/Sink: весь системный звук
    APP_STREAM = "app_stream"          # Stream/Output/Audio: звук приложения
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AudioNode:
    """Узел графа PipeWire, пригодный для захвата."""

    node_id: int
    name: str                       # node.name, техническое имя
    description: str                # человекочитаемое
    kind: NodeKind
    application: str | None         # application.name, если это поток приложения
    media_name: str | None
    rate: int | None
    channels: int | None
    #: Ключ, переживающий перезапуск приложения и смену node_id.
    stable_key: str = field(init=False)

    def __post_init__(self) -> None:
        if self.kind is NodeKind.APP_STREAM:
            key = f"app:{self.application or '?'}|{self.media_name or '?'}"
        else:
            key = f"node:{self.name}"
        object.__setattr__(self, "stable_key", key)

    @property
    def is_capturable(self) -> bool:
        return self.kind is not NodeKind.UNKNOWN

    def label(self) -> str:
        """Строка для выпадающего списка в панели настроек (E7)."""
        if self.kind is NodeKind.APP_STREAM:
            return f"{self.application or 'приложение'} — {self.media_name or self.description}"
        if self.kind is NodeKind.SINK_MONITOR:
            return f"{self.description} (монитор вывода)"
        return self.description or self.name


def _media_class_to_kind(media_class: str | None) -> NodeKind:
    if not media_class:
        return NodeKind.UNKNOWN
    if media_class == "Audio/Source":
        return NodeKind.MICROPHONE
    if media_class in ("Audio/Sink", "Audio/Duplex"):
        return NodeKind.SINK_MONITOR
    if media_class.startswith("Stream/Output/Audio"):
        return NodeKind.APP_STREAM
    return NodeKind.UNKNOWN


def _parse_node(obj: dict[str, Any]) -> AudioNode | None:
    if obj.get("type") != "PipeWire:Interface:Node":
        return None
    info = obj.get("info") or {}
    props: dict[str, Any] = info.get("props") or {}

    kind = _media_class_to_kind(props.get("media.class"))
    if kind is NodeKind.UNKNOWN:
        return None

    # Виртуальные узлы самого сервиса не должны попадать в список: захват
    # собственного вывода даёт петлю обратной связи.
    if str(props.get("application.name", "")).startswith("speech-local"):
        return None

    rate = None
    if isinstance(props.get("audio.rate"), int):
        rate = props["audio.rate"]
    elif isinstance(props.get("node.rate"), str):
        # формат "1/48000"
        try:
            rate = int(str(props["node.rate"]).split("/")[-1])
        except ValueError:
            rate = None

    return AudioNode(
        node_id=int(obj["id"]),
        name=str(props.get("node.name") or f"node-{obj['id']}"),
        description=str(
            props.get("node.description")
            or props.get("node.nick")
            or props.get("node.name")
            or ""
        ),
        kind=kind,
        application=props.get("application.name"),
        media_name=props.get("media.name"),
        rate=rate,
        channels=props.get("audio.channels"),
    )


class PipeWireDiscovery:
    """Опрос графа PipeWire через ``pw-dump``.

    Почему опрос, а не подписка через ``pw-mon``: подписка требует держать
    долгоживущий процесс и разбирать поток событий с частичными обновлениями.
    Выигрыш — доли секунды на обнаружении изменения устройства, что для нашей
    задачи не критично. Периодический ``pw-dump`` проще, не течёт и надёжнее
    переживает перезапуск самого PipeWire. Подписка — кандидат в тир 2.
    """

    def __init__(self, poll_interval_s: float = 3.0) -> None:
        self._poll_interval = poll_interval_s
        self._last: dict[str, AudioNode] = {}
        self._watchers: list[Callable[[list[AudioNode]], None]] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -------------------------------------------------------------- проверки

    @staticmethod
    def available() -> bool:
        """Есть ли инструменты PipeWire в системе.

        Отсутствие — не повод падать: UI обязан показать понятную диагностику
        вместо пустого списка устройств.
        """
        return shutil.which("pw-dump") is not None

    # ------------------------------------------------------------------ опрос

    async def list_nodes(self) -> list[AudioNode]:
        raw = await self._run_pw_dump()
        nodes: list[AudioNode] = []
        for obj in raw:
            try:
                node = _parse_node(obj)
            except Exception:  # noqa: BLE001 — один битый объект не рушит список
                log.debug("не удалось разобрать объект графа: %s", obj.get("id"))
                continue
            if node is not None:
                nodes.append(node)

        nodes.sort(key=lambda n: (n.kind.value, n.label().lower()))
        self._last = {n.stable_key: n for n in nodes}
        return nodes

    async def resolve(self, stable_key: str) -> AudioNode:
        """Найти узел по стабильному ключу. Основной путь для реконнекта."""
        nodes = await self.list_nodes()
        for node in nodes:
            if node.stable_key == stable_key:
                return node
        raise NodeNotFound(f"узел '{stable_key}' отсутствует в графе PipeWire")

    async def try_resolve(self, stable_key: str) -> AudioNode | None:
        try:
            return await self.resolve(stable_key)
        except (NodeNotFound, AudioError):
            return None

    async def _run_pw_dump(self) -> list[dict[str, Any]]:
        if not self.available():
            raise AudioError(
                "pw-dump не найден: PipeWire не установлен или недоступен в PATH"
            )
        proc = await asyncio.create_subprocess_exec(
            "pw-dump",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_PW_DUMP_TIMEOUT_S
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise AudioError("pw-dump не ответил за отведённое время") from exc

        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[:200]
            raise AudioError(f"pw-dump завершился с кодом {proc.returncode}: {detail}")

        try:
            data = json.loads(stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise AudioError("pw-dump вернул неразбираемый JSON") from exc

        if not isinstance(data, list):
            raise AudioError("pw-dump вернул неожиданную структуру")
        return data

    # -------------------------------------------------------------- слежение

    def watch(self, callback: Callable[[list[AudioNode]], None]) -> None:
        """Подписаться на изменения состава узлов.

        Вызывается только при фактическом изменении набора stable_key,
        а не на каждом опросе: иначе UI будет перерисовываться постоянно.
        """
        self._watchers.append(callback)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._watch_loop(), name="pw-discovery")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                previous = set(self._last)
                nodes = await self.list_nodes()
                current = set(self._last)
                if previous != current:
                    added = current - previous
                    removed = previous - current
                    if added:
                        log.info("появились узлы: %s", ", ".join(sorted(added)))
                    if removed:
                        log.warning("исчезли узлы: %s", ", ".join(sorted(removed)))
                    for cb in self._watchers:
                        try:
                            cb(nodes)
                        except Exception:  # noqa: BLE001
                            log.exception("слушатель изменений узлов упал")
            except AudioError as exc:
                log.warning("опрос PipeWire не удался: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("непредвиденный сбой опроса PipeWire")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue
