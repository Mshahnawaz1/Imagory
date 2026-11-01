"""Imagory's look: one palette per colour scheme and one Qt stylesheet.

Everything visual that is not a custom-painted widget is tuned here. The grid paints with the
palette (Base is the paper colour, Highlight the selection outline), so ``apply_theme`` sets both.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from imagory import db


@dataclass(frozen=True)
class Colours:
    forest: str  # title-bar style surfaces, primary buttons
    cream: str  # text on forest
    paper: str  # grid and window background
    surface: str  # toolbar, sidebar, inputs
    surface_alt: str  # hovered and pressed surfaces
    line: str
    text: str
    muted: str
    selection: str  # selected photo outline, focus ring
    accent: str  # lime


LIGHT = Colours(
    forest="#10291F",
    cream="#F7F5EF",
    paper="#EFEDE6",
    surface="#FBFAF6",
    surface_alt="#F3F1EA",
    line="#DCD9CE",
    text="#10291F",
    muted="#6B7568",
    selection="#2F6B3F",
    accent="#BFE24B",
)

DARK = Colours(
    forest="#BFE24B",  # primary buttons flip to lime so they stand out on dark surfaces
    cream="#10291F",
    paper="#0B1A15",
    surface="#10241D",
    surface_alt="#163028",
    line="#24413A",
    text="#F1EFE7",
    muted="#8FA397",
    selection="#BFE24B",
    accent="#BFE24B",
)

VIEWER_BACKGROUND = "#0B1A15"
VIEWER_TEXT = "#F1EFE7"


SYSTEM, LIGHT_MODE, DARK_MODE = "system", "light", "dark"
THEME_MODES = (SYSTEM, LIGHT_MODE, DARK_MODE)
THEME_SETTING = "theme"
_MODE_PROPERTY = "imagory_theme_mode"


def normalize_mode(value: str | None) -> str:
    """A stored value as a known theme mode; anything unknown means System."""
    return value if value in THEME_MODES else SYSTEM


def resolve_dark(mode: str, os_dark: bool) -> bool:
    """Whether to use the dark palette for a theme mode and the OS scheme."""
    mode = normalize_mode(mode)
    return os_dark if mode == SYSTEM else mode == DARK_MODE


def is_dark(app: QApplication, mode: str = SYSTEM) -> bool:
    os_dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    return resolve_dark(mode, os_dark)


def load_mode(conn: sqlite3.Connection) -> str:
    return normalize_mode(db.get_setting(conn, THEME_SETTING))


def save_mode(conn: sqlite3.Connection, mode: str) -> None:
    db.set_setting(conn, THEME_SETTING, normalize_mode(mode))


def current_mode(app: QApplication) -> str:
    return normalize_mode(app.property(_MODE_PROPERTY))


def on_os_scheme_changed(app: QApplication) -> None:
    """Re-theme after the OS scheme flips, but only when following the OS."""
    if current_mode(app) == SYSTEM:
        apply_theme(app)


def make_palette(c: Colours) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: c.paper,
        QPalette.ColorRole.WindowText: c.text,
        QPalette.ColorRole.Base: c.paper,
        QPalette.ColorRole.AlternateBase: c.surface,
        QPalette.ColorRole.Text: c.text,
        QPalette.ColorRole.PlaceholderText: c.muted,
        QPalette.ColorRole.Button: c.surface,
        QPalette.ColorRole.ButtonText: c.text,
        QPalette.ColorRole.ToolTipBase: c.forest,
        QPalette.ColorRole.ToolTipText: c.cream,
        QPalette.ColorRole.Highlight: c.selection,
        QPalette.ColorRole.HighlightedText: c.cream if c is LIGHT else c.forest,
        QPalette.ColorRole.Link: c.selection,
    }
    for role, colour in roles.items():
        p.setColor(role, QColor(colour))
    return p


def stylesheet(c: Colours) -> str:
    return f"""
QMainWindow, QDialog {{ background: {c.paper}; }}
QMenuBar {{ background: {c.surface}; border-bottom: 1px solid {c.line}; }}
QMenuBar::item {{ padding: 5px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {c.surface_alt}; border-radius: 4px; }}
QMenu {{ background: {c.surface}; border: 1px solid {c.line}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c.forest}; color: {c.cream}; }}
QMenu::separator {{ height: 1px; background: {c.line}; margin: 4px 6px; }}
QToolBar {{ background: {c.surface}; border: 0; border-bottom: 1px solid {c.line};
    padding: 4px 8px; spacing: 6px; }}
QToolButton {{ border: 1px solid transparent; border-radius: 6px; padding: 5px 10px; }}
QToolButton:hover {{ background: {c.surface_alt}; border-color: {c.line}; }}
QToolButton:checked {{ background: {c.surface_alt}; border-color: {c.line}; }}
QToolButton#filter-source {{ background: {c.surface}; border: 1px solid {c.line};
    padding: 4px 10px; }}
QToolButton::menu-indicator {{ image: none; }}
QPushButton {{ background: {c.surface}; border: 1px solid {c.line}; border-radius: 6px;
    padding: 5px 12px; }}
QPushButton:hover {{ background: {c.surface_alt}; }}
QPushButton:default, QPushButton#primary {{ background: {c.forest}; color: {c.cream};
    border-color: {c.forest}; padding: 7px 22px; }}
QPushButton:disabled {{ color: {c.muted}; }}
QLineEdit, QComboBox, QDateEdit {{ background: {c.surface}; border: 1px solid {c.line};
    border-radius: 6px; padding: 4px 8px; selection-background-color: {c.selection}; }}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus {{ border-color: {c.selection}; }}
QComboBox QAbstractItemView {{ background: {c.surface}; border: 1px solid {c.line};
    selection-background-color: {c.forest}; selection-color: {c.cream}; }}
QCheckBox {{ spacing: 6px; }}
QStatusBar {{ background: {c.surface}; border-top: 1px solid {c.line}; color: {c.muted}; }}
QStatusBar::item {{ border: 0; }}
QProgressBar {{ border: 1px solid {c.line}; border-radius: 4px; background: {c.surface_alt};
    text-align: center; height: 14px; }}
QProgressBar::chunk {{ background: {c.selection}; border-radius: 3px; }}
QToolTip {{ background: {c.forest}; color: {c.cream}; border: 0; padding: 4px 8px; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c.line}; border-radius: 4px; min-height: 36px;
    margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: {c.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QListWidget, QTableWidget, QTreeWidget {{ background: {c.surface}; border: 1px solid {c.line};
    border-radius: 6px; }}
QListWidget::item {{ padding: 4px 6px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {c.forest}; color: {c.cream}; }}
QHeaderView::section {{ background: {c.surface_alt}; border: 0; border-bottom: 1px solid {c.line};
    padding: 4px 8px; }}
QSlider::groove:horizontal {{ height: 4px; background: {c.line}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {c.forest}; width: 12px; margin: -5px 0;
    border-radius: 6px; }}
QFrame#sidebar {{ background: {c.surface}; border: 0; border-right: 1px solid {c.line}; }}
QFrame#sidebar QListWidget {{ background: transparent; border: 0; }}
QLabel#sidebar-heading {{ color: {c.muted}; font-size: 8pt; font-weight: 600;
    letter-spacing: 1px; padding: 10px 10px 2px 10px; }}
QPushButton#chip {{ background: {c.surface_alt}; border: 1px solid {c.line};
    border-radius: 11px; padding: 2px 10px; font-weight: 400; color: {c.text}; }}
QPushButton#chip:hover {{ border-color: {c.selection}; }}
QWidget#filter-bar {{ background: {c.surface}; border-bottom: 1px solid {c.line}; }}
"""


def apply_theme(app: QApplication, mode: str | None = None) -> None:
    """Use the Fusion style (it follows the palette and stylesheet on every platform) and
    the colours for the chosen mode (System follows the OS). Without ``mode`` the mode
    applied last is kept."""
    mode = current_mode(app) if mode is None else normalize_mode(mode)
    app.setProperty(_MODE_PROPERTY, mode)
    app.setStyle("Fusion")
    colours = DARK if is_dark(app, mode) else LIGHT
    app.setPalette(make_palette(colours))
    font = app.font()
    if font.pointSizeF() < 9.5:
        font.setPointSizeF(9.5)
    app.setFont(font)
    app.setStyleSheet(stylesheet(colours))
