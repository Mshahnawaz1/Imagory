"""Library folder resolution and bundled-resource lookup."""

from __future__ import annotations

import os
import sys
from pathlib import Path

LIBRARY_ENV = "IMAGORY_LIBRARY"
DB_NAME = "imagory.db"
CLEANUP_DIR_NAME = ".imagory"


def library_dir() -> Path:
    """Return the library folder (database, thumbnails, logs), without creating it.

    ``IMAGORY_LIBRARY`` overrides the platform default, for tests and development.
    """
    override = os.environ.get(LIBRARY_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Imagory"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "imagory"


def ensure_library_dir() -> Path:
    """Return the library folder, creating it and its subfolders if needed."""
    root = library_dir()
    for sub in (root, thumbs_dir(root), logs_dir(root)):
        sub.mkdir(parents=True, exist_ok=True)
    return root


def db_path(root: Path) -> Path:
    return root / DB_NAME


def thumbs_dir(root: Path) -> Path:
    return root / "thumbs"


def logs_dir(root: Path) -> Path:
    return root / "logs"


def cleanup_dir(output: Path) -> Path:
    """Return the hidden ``.imagory`` folder inside a cleanup output folder."""
    return output / CLEANUP_DIR_NAME


def cleanup_db_path(output: Path) -> Path:
    """Return the database path inside a cleanup output folder."""
    return cleanup_dir(output) / DB_NAME


def cleanup_thumbs_dir(output: Path) -> Path:
    """Return the thumbnail folder inside a cleanup output folder."""
    return cleanup_dir(output) / "thumbs"


def cleanup_reports_dir(output: Path) -> Path:
    """Return the report folder inside a cleanup output folder."""
    return cleanup_dir(output) / "reports"


def metadata_json_path(output: Path) -> Path:
    """Return the path of the plain-text ``metadata.json`` in a cleanup output folder."""
    return cleanup_dir(output) / "metadata.json"


def resource_path(relative: str) -> Path:
    """Locate a bundled resource in both normal runs and PyInstaller builds."""
    base = getattr(sys, "_MEIPASS", None)
    if base is not None:
        return Path(base) / relative
    return Path(__file__).resolve().parent / relative
