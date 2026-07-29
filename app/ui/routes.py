"""app/ui/routes.py — route setup for E1 UI SSE server."""

from pathlib import Path

from aiohttp import web


def setup_routes(app: web.Application, ui_server) -> None:
    """Register all UI server routes on the given aiohttp application."""
    app.router.add_get("/events", ui_server._events_handler)
    app.router.add_get("/api/snapshot", ui_server._snapshot_handler)
    app.router.add_post("/api/session/start", ui_server._session_start_handler)
    app.router.add_post("/api/session/stop", ui_server._session_stop_handler)
    app.router.add_post("/api/privacy", ui_server._privacy_handler)
    app.router.add_get("/health", ui_server._health_handler)
    app.router.add_get("/ready", ui_server._ready_handler)
    app.router.add_post("/api/clipboard", ui_server._clipboard_handler)
    app.router.add_get("/api/sessions", ui_server._sessions_list_handler)
    app.router.add_get("/api/sessions/{session_id}", ui_server._session_get_handler)
    app.router.add_get(
        "/api/sessions/{session_id}/export.{fmt}", ui_server._session_export_handler
    )
    app.router.add_post("/api/key", ui_server._key_put_handler)
    app.router.add_post("/api/key/revoke", ui_server._key_revoke_handler)
    app.router.add_post("/api/languages", ui_server._languages_handler)
    app.router.add_get("/api/library", ui_server._library_list_handler)
    app.router.add_post("/api/library", ui_server._library_upsert_handler)
    app.router.add_delete("/api/library/{context_id}", ui_server._library_delete_handler)
    # Static files (optional)
    # Note: static path is set in UiServer.__init__; we rely on that.
    # If you want to configure static path here, pass it via ui_server._config.static_path.
    # For now, we assume the static folder is served by the same mechanism as in UiServer.__init__.
    # We'll add a static route if the config has a static_path attribute.
    static_path = getattr(ui_server._config, "static_path", None)
    if static_path:
        index_path = Path(static_path) / "index.html"

        async def _index_handler(request: web.Request) -> web.FileResponse:
            return web.FileResponse(index_path)

        app.router.add_get("/", _index_handler)
        app.router.add_static("/static", static_path, name="static")
