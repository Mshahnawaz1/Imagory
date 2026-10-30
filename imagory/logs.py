"""Rotating log file in the library's ``logs`` folder. Logs never leave the machine."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_FILE = "imagory.log"
_FORMAT = "%(asctime)s %(levelname)s %(processName)s %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    """Attach a rotating file handler to the root logger and return the log file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / LOG_FILE
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_imagory", False):
            root.removeHandler(handler)
            handler.close()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler._imagory = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    return path
