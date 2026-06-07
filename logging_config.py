"""Centralized logging for NewLyricsJukebox.

Simple console logging plus an optional rotating file handler when
``NLJ_LOGS_DIR`` is set (the Home Assistant add-on points it at /config/logs).
"""

import logging
import logging.handlers
import os
from pathlib import Path

CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s (%(filename)s:%(lineno)d): %(message)s"

_configured = False


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once. Idempotent."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    root.addHandler(console)

    logs_dir = os.getenv("NLJ_LOGS_DIR")
    if logs_dir:
        try:
            path = Path(logs_dir)
            path.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path / "newlyricsjukebox.log",
                maxBytes=2_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
            root.addHandler(file_handler)
        except (OSError, PermissionError) as exc:  # pragma: no cover - best effort
            root.warning("Could not set up file logging: %s", exc)

    # The MA client logs the auth token at DEBUG — keep it quiet.
    logging.getLogger("music_assistant_client").setLevel(logging.WARNING)
    logging.getLogger("shazamio").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
