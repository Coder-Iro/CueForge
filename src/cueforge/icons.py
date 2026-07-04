"""Application icon helpers."""

from __future__ import annotations

from importlib.resources import files

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication


def cueforge_icon() -> QIcon:
    icon_path = files("cueforge.assets").joinpath("cueforge.ico")
    return QIcon(str(icon_path))


def apply_app_icon(app: QApplication) -> None:
    icon = cueforge_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
