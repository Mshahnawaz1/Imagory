"""Viewer info panel (PRD §8.6) and "Show in Explorer".

Everything here only reads: revealing a file asks the file manager to open the folder
that contains it and never touches the file itself.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from imagory.db import PhotoInfo, PhotoLocation

log = logging.getLogger(__name__)

DATE_SOURCE_LABELS = {
    "takeout_json": "Takeout JSON",
    "exif": "EXIF capture date",
    "filename": "file name",
    "mtime": "file modified time",
}


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size:,} bytes"
    value = size / 1024
    for unit in ("KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size:,} bytes"


def format_coordinates(latitude: float | None, longitude: float | None) -> str:
    if latitude is None or longitude is None:
        return "Not known"
    ns = "N" if latitude >= 0 else "S"
    ew = "E" if longitude >= 0 else "W"
    return f"{abs(latitude):.5f}° {ns}, {abs(longitude):.5f}° {ew}"


def format_utc(value: str | None) -> str:
    """A UTC timestamp ("2019-01-31T23:59:59Z") for display; "" when there is none."""
    if not value:
        return ""
    return value.replace("T", " ").removesuffix("Z") + " UTC"


def format_people(names: tuple[str, ...] | list[str]) -> str:
    return ", ".join(name for name in names if name)


def full_path(location: PhotoLocation) -> Path:
    return Path(location.source_root).joinpath(*location.relative_path.split("/"))


def is_wsl() -> bool:
    return sys.platform.startswith("linux") and bool(os.environ.get("WSL_DISTRO_NAME"))


def reveal_argv(
    path: Path, platform: str | None = None, wsl_path: str | None = None
) -> list[str] | str | None:
    """The command that shows ``path`` selected in the platform's file manager, or None
    when there is no way to select a file (the caller then just opens the folder).

    ``wsl_path`` is the Windows form of the path when running under WSL.

    On Windows this is a command line, not a list: ``subprocess`` would quote the whole
    ``/select,<path>`` argument when the path has a space, and Explorer then ignores the
    switch and opens a default folder. Only the path may be quoted (Windows paths cannot
    contain ``"``).
    """
    platform = platform or sys.platform  # read at call time so tests can patch it
    if platform == "win32":
        return f'explorer /select,"{os.path.normpath(path)}"'
    if platform == "darwin":
        return ["open", "-R", str(path)]
    if wsl_path:
        return ["explorer.exe", f"/select,{wsl_path}"]
    return None


def show_in_explorer(path: Path) -> bool:
    """Open the folder containing ``path`` with the file selected where possible.

    Windows: Explorer with the file selected. macOS: Finder. WSL: Windows Explorer via
    ``wslpath``. Other Linux: the default handler opens the containing folder.
    Returns False when nothing could be launched. Never writes to ``path``.
    """
    wsl_path = None
    if is_wsl():
        try:
            out = subprocess.run(
                ["wslpath", "-w", str(path)], capture_output=True, text=True, timeout=5
            )
            wsl_path = out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("wslpath failed: %s", exc)
    argv = reveal_argv(path, wsl_path=wsl_path)
    if argv:
        try:
            subprocess.Popen(argv)  # Explorer exits non-zero even on success; don't wait
            return True
        except OSError as exc:
            log.info("Could not run %s: %s", argv[0], exc)
    folder = path.parent
    if not folder.is_dir():
        return False
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def describe(info: PhotoInfo | None) -> str:
    """The panel text as HTML; a plain function so it can be tested without a window."""
    if info is None:
        return "No details available."

    def row(name: str, value: str) -> str:
        return f"<p><b>{escape(name)}</b><br>{escape(value)}</p>"

    if info.taken_at_local:
        origin = DATE_SOURCE_LABELS.get(info.date_source or "", info.date_source or "unknown")
        date = f"{info.taken_at_local.replace('T', ' ')} (from {origin})"
    else:
        date = "Unknown date"
    parts = [row("Date", date), row("Location", format_coordinates(info.latitude, info.longitude))]
    if info.people:
        parts.append(row("People", format_people(info.people)))
    if info.description:
        parts.append(row("Caption", info.description))
    if info.favorite:
        parts.append(row("Favourite", "Yes"))
    if info.uploaded_at_utc:
        parts.append(row("Added to Google Photos", format_utc(info.uploaded_at_utc)))
    primary = info.primary
    if primary:
        parts.append(row("Source", primary.source_label))
        parts.append(row("Path", str(full_path(primary))))
        parts.append(row("Size", format_size(primary.size)))
        if primary.status == "missing":
            parts.append(row("Status", "This file is missing from its source"))
    dims = f"{info.width} × {info.height}" if info.width and info.height else "Unknown"
    if info.format:
        dims += f" ({info.format})"
    parts.append(row("Dimensions", dims))
    if info.others:
        lines = "".join(
            f"<br>{escape(o.source_label)}: {escape(o.relative_path)}"
            + (" (missing)" if o.status == "missing" else "")
            for o in info.others
        )
        parts.append(f"<p><b>Also found in</b>{lines}</p>")
    return "".join(parts)


class InfoPanel(QWidget):
    """Read-only details of the photo showing in the viewer."""

    reveal_requested = Signal(object)  # PhotoLocation

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(320)
        self._primary: PhotoLocation | None = None
        self.details = QLabel(self)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setWordWrap(True)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.reveal_button = QPushButton("Show in Explorer", self)
        self.reveal_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.reveal_button.clicked.connect(self._reveal)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self.details)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.reveal_button)
        self.set_info(None)

    def set_info(self, info: PhotoInfo | None) -> None:
        self._primary = info.primary if info else None
        self.reveal_button.setEnabled(self._primary is not None)
        self.details.setText(describe(info))

    def _reveal(self) -> None:
        if self._primary is not None:
            self.reveal_requested.emit(self._primary)
