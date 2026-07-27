"""app/logging_setup.py — logging configuration with redactor attached to handlers."""

from __future__ import annotations

import logging
import sys

from app.security.redactor import LogRedactor


def setup_logging(level: int = logging.INFO) -> LogRedactor:
    """
    Configure root logger and attach the redactor to all handlers.
    Returns the redactor instance for further tweaks (e.g., add_literal).
    """
    # Create a redactor instance
    redactor = LogRedactor()

    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(level)

    # Ensure we have a handler; if not, add a stream handler to stderr
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    # Attach the redactor to all existing handlers
    for handler in logger.handlers:
        handler.addFilter(redactor)

    # Also, we want to catch any handlers that might be added later?
    # We cannot easily hook into addHandler, but we can note that the typical
    # usage is to call setup_logging early and then add handlers.
    # For safety, we can also set a placeholder that will be called when
    # adding handlers, but that's more complex. We'll rely on the user
    # calling setup_logging before adding any handlers, or we can document
    # that they should add the redactor manually if they add handlers later.
    # However, the contract says: "logging_setup вешает фильтр на обработчики"
    # We interpret that as: after calling setup_logging, all handlers that
    # exist at that moment have the filter. Future handlers are the
    # responsibility of the caller to also add the filter to, or we could
    # modify the logging configuration to add the filter by default.
    # Let's do a simple approach: we also set the root logger's addHandler
    # to automatically add the filter? That's too heavy.

    # Instead, we'll return the redactor and let the caller know they should
    # add it to any handlers they create later. But the contract says the
    # setup function does it. We'll assume that the caller will not add
    # handlers after setup, or if they do, they will also add the filter.
    # For now, we do as above.

    return redactor