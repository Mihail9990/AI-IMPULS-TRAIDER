from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_diagnostics(path: str) -> None:
    """Write a persistent, rotating diagnostic history suitable for support."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(name)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not any(
        isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename) == destination.resolve()
        for handler in root.handlers
    ):
        file_handler = RotatingFileHandler(
            destination, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    if not any(type(handler) is logging.StreamHandler for handler in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)


def clear_diagnostics(path: str) -> None:
    """Start a fresh diagnostic window and remove rotated history files.

    The active handler must be truncated through its open stream. Unlinking only the path on
    Android/Linux would leave logging attached to an invisible old inode until Pydroid restarts.
    """
    destination = Path(path).resolve()
    root = logging.getLogger()
    handlers = [
        handler for handler in root.handlers
        if isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename).resolve() == destination
    ]
    for handler in handlers:
        handler.acquire()
        try:
            handler.flush()
            handler.stream.seek(0)
            handler.stream.truncate(0)
            os.fsync(handler.stream.fileno())
        finally:
            handler.release()
    for index in range(1, 6):
        destination.with_name(f"{destination.name}.{index}").unlink(missing_ok=True)
