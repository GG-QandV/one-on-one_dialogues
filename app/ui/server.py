"""app/ui/server.py — E1 UI SSE server (aiohttp)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Set

from aiohttp import web

from app.config import STT_PROVIDER_CHOICES

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
        """Return UI‑specific snapshot — nested under 'ui' for compat, no app recursion."""
        return {
            "ui": {
                "client_count": self._client_count,
                "sequence": self._sequence,
                "lost_events": self._lost_events,
            }
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
        if hasattr(self._app, "snapshot"):
            snap = self._app.snapshot()
        else:
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

    # ---------------------------------------------------------- E8 history

    async def _sessions_list_handler(self, request: web.Request) -> web.Response:
        db = getattr(self._app, "db", None)
        if db is None:
            return web.json_response({"error": "not implemented"}, status=501)
        rows = await db.fetch_all(
            "SELECT id, started_at, ended_at, meeting_title, status, "
            "default_privacy_profile, mode FROM sessions "
            "ORDER BY started_at DESC"
        )
        return web.json_response({"sessions": [dict(row) for row in rows]})

    async def _session_get_handler(self, request: web.Request) -> web.Response:
        db = getattr(self._app, "db", None)
        if db is None:
            return web.json_response({"error": "not implemented"}, status=501)
        session_id = request.match_info["session_id"]
        session_row = await db.fetch_one(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        if session_row is None:
            return web.json_response({"error": "not found"}, status=404)
        segment_rows = await db.fetch_all(
            "SELECT s.*, a.role AS role FROM segments s "
            "JOIN audio_streams a ON a.id = s.stream_id "
            "WHERE s.session_id = ? ORDER BY s.t_start_ms",
            (session_id,),
        )
        draft_rows = await db.fetch_all(
            "SELECT * FROM draft_answers WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        return web.json_response(
            {
                "session": dict(session_row),
                "segments": [self._segment_to_dict(row) for row in segment_rows],
                "drafts": [self._draft_to_dict(row) for row in draft_rows],
            }
        )

    async def _session_export_handler(self, request: web.Request) -> web.Response:
        db = getattr(self._app, "db", None)
        if db is None:
            return web.json_response({"error": "not implemented"}, status=501)
        session_id = request.match_info["session_id"]
        fmt = request.match_info["fmt"]
        if fmt not in ("txt", "srt", "vtt", "json"):
            return web.json_response({"error": "unsupported format"}, status=400)
        session_row = await db.fetch_one(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        if session_row is None:
            return web.json_response({"error": "not found"}, status=404)
        segment_rows = await db.fetch_all(
            "SELECT s.*, a.role AS role FROM segments s "
            "JOIN audio_streams a ON a.id = s.stream_id "
            "WHERE s.session_id = ? ORDER BY s.t_start_ms",
            (session_id,),
        )

        if fmt == "txt":
            from app.exports.txt import to_txt
            body, content_type = to_txt(segment_rows), "text/plain"
        elif fmt == "srt":
            from app.exports.subtitles import to_srt
            body, content_type = to_srt(segment_rows), "application/x-subrip"
        elif fmt == "vtt":
            from app.exports.subtitles import to_vtt
            body, content_type = to_vtt(segment_rows), "text/vtt"
        else:
            from app.exports.json_export import to_json
            draft_rows = await db.fetch_all(
                "SELECT * FROM draft_answers WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            )
            body = to_json(dict(session_row), segment_rows, draft_rows)
            content_type = "application/json"

        return web.Response(
            text=body,
            content_type=content_type,
            charset="utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="session_{session_id}.{fmt}"'
            },
        )

    @staticmethod
    def _segment_to_dict(row: Any) -> dict[str, Any]:
        d = dict(row)
        edit_log = d.get("edit_log_json")
        if edit_log:
            try:
                d["edit_log_json"] = json.loads(edit_log)
            except (json.JSONDecodeError, TypeError):
                d["edit_log_json"] = None
        return d

    @staticmethod
    def _draft_to_dict(row: Any) -> dict[str, Any]:
        d = dict(row)
        sources = d.get("sources_json")
        if sources:
            try:
                d["sources_json"] = json.loads(sources)
            except (json.JSONDecodeError, TypeError):
                d["sources_json"] = []
        return d

    # -------------------------------------------------------- E7 settings

    async def _key_put_handler(self, request: web.Request) -> web.Response:
        keystore = getattr(self._app, "keystore", None)
        if keystore is None:
            return web.json_response({"error": "not implemented"}, status=501)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        provider = data.get("provider")
        key = data.get("key")
        if not provider or not key:
            return web.json_response({"error": "provider and key required"}, status=400)
        from app.errors import ProviderAuthError

        try:
            keystore.put(provider, key)
        except ProviderAuthError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"masked": keystore.masked(provider)})

    async def _key_revoke_handler(self, request: web.Request) -> web.Response:
        keystore = getattr(self._app, "keystore", None)
        if keystore is None:
            return web.json_response({"error": "not implemented"}, status=501)
        try:
            data = await request.json()
        except Exception:
            data = {}
        keystore.revoke(data.get("provider"))
        return web.Response(status=204)

    async def _languages_handler(self, request: web.Request) -> web.Response:
        if not hasattr(self._app, "set_stream_language"):
            return web.json_response({"error": "not implemented"}, status=501)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(data, dict) or not data:
            return web.json_response({"error": "expected {role: language}"}, status=400)
        for role, lang in data.items():
            if role not in ("microphone", "meeting") or not lang or not isinstance(lang, str):
                return web.json_response(
                    {"error": f"invalid role or language: {role}"}, status=400
                )
        for role, lang in data.items():
            self._app.set_stream_language(role, lang)
        return web.Response(status=204)

    # -------------------------------------------------------- STT provider (E7)

    async def _stt_get_handler(self, request: web.Request) -> web.Response:
        """GET /api/stt — цепочка фолбэков и статус ключей (без самих ключей)."""
        cfg = getattr(self._app, "stt_config", None)
        if cfg is None:
            return web.json_response({"error": "not implemented"}, status=501)
        refresh = getattr(self._app, "refresh_stt_keys", None)
        if callable(refresh):
            refresh()  # файловые ключи ~/.secrets → KeyStore для статуса
        keystore = getattr(self._app, "keystore", None)

        chain = []
        for entry in cfg.chain:
            item = {
                "provider": entry.provider,
                "model": entry.model,
                "endpoint": entry.endpoint,
                "key_name": entry.key_name,
                "fallback_model": entry.fallback_model,
                "device": entry.device,
                "timeout_s": entry.timeout_s,
                "cooldown_s": entry.cooldown_s,
            }
            if entry.provider != "local_whisper":
                has_key = bool(keystore and entry.key_name and keystore.has(entry.key_name))
                item["key_present"] = has_key
                item["key_masked"] = (
                    keystore.masked(entry.key_name) if has_key and keystore else None
                )
            chain.append(item)

        return web.json_response(
            {
                "mode": cfg.mode,
                "json_output": cfg.json_output,
                "language_autodetect": cfg.language_autodetect,
                "choices": list(STT_PROVIDER_CHOICES),
                "chain": chain,
            }
        )

    async def _stt_put_handler(self, request: web.Request) -> web.Response:
        """POST /api/stt — заменить цепочку фолбэков.

        Ключи уходят в KeyStore по своим `key_name`, НЕ в config.toml. Порядок
        звеньев и инвариант §8.7 проверяет `config.update` (та же валидация,
        что при ручной правке файла), затем цепочка пересобирается без
        перезапуска процесса.
        """
        from app.errors import ProviderAuthError, SpeechLocalError

        cfg = getattr(self._app, "stt_config", None)
        if cfg is None or not hasattr(self._app, "update_config"):
            return web.json_response({"error": "not implemented"}, status=501)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "expected JSON object"}, status=400)

        chain = data.get("chain")
        if not isinstance(chain, list) or not chain:
            return web.json_response({"error": "chain must be a non-empty list"}, status=400)

        keys = data.get("keys") or {}
        if not isinstance(keys, dict):
            return web.json_response({"error": "keys must be an object"}, status=400)
        keystore = getattr(self._app, "keystore", None)
        for key_name, api_key in keys.items():
            if not api_key:
                continue
            if keystore is None:
                return web.json_response({"error": "keystore not available"}, status=501)
            try:
                keystore.put(key_name, api_key)
            except ProviderAuthError as exc:
                return web.json_response({"error": str(exc)}, status=400)

        try:
            new_cfg = await self._app.update_config({"stt": {"chain": chain}})
        except SpeechLocalError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        await self._app.reload_stt_provider(new_cfg.stt)
        return web.Response(status=204)

    async def _library_list_handler(self, request: web.Request) -> web.Response:
        library = getattr(self._app, "library", None)
        if library is None:
            return web.json_response({"error": "not implemented"}, status=501)
        items = await library.list()
        return web.json_response(
            [
                {
                    "id": item.id,
                    "name": item.name,
                    "domain": item.domain,
                    "token_estimate": item.token_estimate,
                    "updated_at": item.updated_at,
                }
                for item in items
            ]
        )

    async def _library_upsert_handler(self, request: web.Request) -> web.Response:
        library = getattr(self._app, "library", None)
        if library is None:
            return web.json_response({"error": "not implemented"}, status=501)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        name = data.get("name")
        if not name:
            return web.json_response({"error": "name required"}, status=400)
        from app.drafts.library import LibraryTooLarge
        from app.errors import InvariantViolation

        try:
            context_id = await library.upsert(
                name, data.get("domain"), data.get("content_text", "")
            )
        except LibraryTooLarge as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except InvariantViolation as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"id": context_id})

    async def _library_delete_handler(self, request: web.Request) -> web.Response:
        library = getattr(self._app, "library", None)
        if library is None:
            return web.json_response({"error": "not implemented"}, status=501)
        from app.errors import InvariantViolation

        context_id = request.match_info["context_id"]
        try:
            await library.delete(context_id)
        except InvariantViolation as exc:
            msg = str(exc)
            status = 404 if "not_found" in msg else 409
            return web.json_response({"error": msg}, status=status)
        return web.Response(status=204)
