"""Application icon, loaded from the bundled PNG and ICO (no QtSvg needed)."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon

from imagory import paths


def asset_path(name: str) -> Path:
    """Locate a file in the top-level ``assets`` folder (dev tree or PyInstaller bundle)."""
    if getattr(sys, "_MEIPASS", None) is not None:
        return paths.resource_path(f"assets/{name}")
    return Path(__file__).resolve().parents[2] / "assets" / name


def app_icon() -> QIcon:
    icon = QIcon()
    for name in ("imagory-256.png", "imagory.ico"):
        path = asset_path(name)
        if path.is_file():
            icon.addFile(str(path))
    return icon
