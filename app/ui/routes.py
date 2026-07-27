"""app/ui/routes.py — route setup for E1 UI SSE server."""

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
    # Static files (optional)
    # Note: static path is set in UiServer.__init__; we rely on that.
    # If you want to configure static path here, pass it via ui_server._config.static_path.
    # For now, we assume the static folder is served by the same mechanism as in UiServer.__init__.
    # We'll add a static route if the config has a static_path attribute.
    static_path = getattr(ui_server._config, "static_path", None)
    if static_path:
        app.router.add_static("/static", static_path, name="static")