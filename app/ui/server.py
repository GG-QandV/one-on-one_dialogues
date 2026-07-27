"""app/ui/server.py — E1 UI SSE server (aiohttp)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional, TYPE_CHECKING, Set

from aiohttp import web

if TYPE_CHECKING:
    from app.main import Application

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UiConfig:
    host: str = "127.0.0.1"
    port: int = 8790
    heartbeat_s: float = 15.0
    queue_max: int = 500
    static_path: str = "app/ui/static"  # default static folder


class EventType(str):
    SEGMENT_PARTIAL = "segment.partial"
    SEGMENT_FINAL = "segment.final"
    SEGMENT_TRANSLATED = "segment.translated"
    DRAFT_CREATED = "draft.created"
    DRAFT_TRANSLATED = "draft.translated"
    PRIVACY_CHANGED = "privacy.changed"
    STATUS = "status"


@dataclass(frozen=True, slots=True)
class UiEvent:
    type: EventType
    data: dict[str, Any]
    sequence: int  # monotonic, shared across all types


class UiServer:
    def __init__(self, app: "Application", config: UiConfig | None = None):
        self._app = app
        self._config = config or UiConfig()
        # Validate host is loopback
        if self._config.host not in ("127.0.0.1", "::1", "localhost"):
            log.warning(
                "UI server host %s is not a loopback address; "
                "this may leak sensitive data over the network",
                self._config.host,
            )
        self._app_web = web.Application()
        # Set up routes via routes.py
        from .routes import setup_routes

        setup_routes(self._app_web, self)
        # NOTE: static route is already added in setup_routes if static_path is set.
        # Do not add it again here to avoid duplicate resource registration.
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._client_queues: Dict[int, asyncio.Queue[Optional[UiEvent]]] = {}
        self._client_queues_lock = asyncio.Lock()
        self._sequence = 0
        self._sequence_lock = asyncio.Lock()
        self._client_count = 0
        self._lost_events = 0  # counter for dropped events due to queue overflow
        self._publish_tasks: Set[asyncio.Task] = set()  # track background publish tasks

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app_web)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await self._site.start()
        log.info("UI server started on http://%s:%s", self._config.host, self._config.port)

    async def stop(self) -> None:
        # Cancel all pending publish tasks
        for task in self._publish_tasks:
            if not task.done():
                task.cancel()
        if self._publish_tasks:
            await asyncio.gather(*self._publish_tasks, return_exceptions=True)
        self._publish_tasks.clear()

        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        # Cancel any remaining client connections
        async with self._client_queues_lock:
            for q in self._client_queues.values():
                await q.put(None)  # Sentinel to break consumer
            self._client_queues.clear()
            self._client_count = 0
        log.info("UI server stopped")

    def publish(self, event_type: EventType, data: dict[str, Any]) -> None:
        """Non‑blocking publish from the processing pipeline."""
        # Schedule the async put‑loop; we don't await because publish must not block.
        task = asyncio.create_task(self._publish_internal(event_type, data))
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    async def _publish_internal(
        self, event_type: EventType, data: dict[str, Any]
    ) -> None:
        async with self._sequence_lock:
            self._sequence += 1
            seq = self._sequence
        event = UiEvent(type=event_type, data=data, sequence=seq)
        async with self._client_queues_lock:
            queues = list(self._client_queues.values())
        for q in queues:
            try:
                # If queue is full, drop the oldest item (as per spec)
                if q.qsize() >= self._config.queue_max:
                    try:
                        _ = q.get_nowait()
                        self._lost_events += 1
                    except asyncio.QueueEmpty:
                        pass
                await q.put(event)
            except Exception:  # pragma: no cover – defensive
                log.exception("Failed to enqueue UI event for a client")

    def snapshot(self) -> dict[str, Any]:
        """Return UI‑specific snapshot (client count, etc.) plus app snapshot."""
        base = self._app.snapshot() if hasattr(self._app, "snapshot") else {}
        return {
            **base,
            "ui": {
                "client_count": self._client_count,
                "sequence": self._sequence,
                "lost_events": self._lost_events,
            },
        }

    # ------------------------------------------------------------------ HTTP handlers
    async def _events_handler(self, request: web.Request) -> web.StreamResponse:
        # SSE endpoint
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        # Create a queue for this client
        q: asyncio.Queue[Optional[UiEvent]] = asyncio.Queue(
            maxsize=self._config.queue_max
        )
        client_id = id(q)
        async with self._client_queues_lock:
            self._client_queues[client_id] = q
            self._client_count = len(self._client_queues)
        try:
            while True:
                try:
                    # Wait for event with timeout for heartbeat
                    try:
                        event = await asyncio.wait_for(
                            q.get(), timeout=self._config.heartbeat_s
                        )
                    except asyncio.TimeoutError:
                        # Send heartbeat comment
                        await resp.write(b": ping\n\n")
                        continue
                    if event is None:  # Sentinel to close
                        break
                    # Format SSE frame
                    data_json = json.dumps(event.data, ensure_ascii=False)
                    line = f"event: {event.type}\ndata: {data_json}\nid: {event.sequence}\n\n"
                    await resp.write(line.encode("utf-8"))
                except (ConnectionResetError, BrokenPipeError):
                    break
        finally:
            async with self._client_queues_lock:
                self._client_queues.pop(client_id, None)
                self._client_count = len(self._client_queues)
        return resp

    async def _snapshot_handler(self, request: web.Request) -> web.Response:
        snap = self.snapshot()
        return web.json_response(snap)

    async def _session_start_handler(self, request: web.Request) -> web.Response:
        # Delegate to app.start_session if exists
        if hasattr(self._app, "start_session"):
            # Expect optional JSON body with meeting_title
            try:
                data = await request.json()
                meeting_title = data.get("meeting_title")
            except Exception:
                meeting_title = None
            session_id = await self._app.start_session(meeting_title=meeting_title)  # type: ignore
            return web.json_response({"session_id": session_id})
        return web.json_response({"error": "not implemented"}, status=501)

    async def _session_stop_handler(self, request: web.Request) -> web.Response:
        if hasattr(self._app, "stop_session"):
            await self._app.stop_session()  # type: ignore
            return web.Response(status=204)
        return web.json_response({"error": "not implemented"}, status=501)

    async def _privacy_handler(self, request: web.Request) -> web.Response:
        # Expect JSON with profile: "open"|"confidential"
        try:
            data = await request.json()
            profile_str = data.get("profile")
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if profile_str not in ("open", "confidential"):
            return web.json_response({"error": "invalid profile"}, status=400)
        from app.privacy import PrivacyProfile

        target = (
            PrivacyProfile.OPEN
            if profile_str == "open"
            else PrivacyProfile.CONFIDENTIAL
        )
        if hasattr(self._app, "privacy"):
            await self._app.privacy.switch(target)  # type: ignore
            return web.Response(status=204)
        return web.json_response({"error": "not implemented"}, status=501)

    async def _health_handler(self, request: web.Request) -> web.Response:
        return web.Response(status=200, text="OK")

    async def _ready_handler(self, request: web.Request) -> web.Response:
        if hasattr(self._app, "is_ready") and await self._app.is_ready():  # type: ignore
            return web.Response(status=200, text="READY")
        return web.Response(status=200, text="READY")

    async def _clipboard_handler(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)
        text = data.get("text", "")
        if not text:
            return web.json_response({"ok": False}, status=400)
        from app.delivery.clipboard import copy
        ok = await copy(text)
        return web.json_response({"ok": ok})