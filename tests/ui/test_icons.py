import os
import xml.etree.ElementTree as ET

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imagory.ui.icons import app_icon, asset_path  # noqa: E402


def test_app_icon_not_null():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None
    assert not app_icon().isNull()


@pytest.mark.parametrize("name", ["imagory-mark.svg", "imagory-logo.svg", "imagory-logo-dark.svg"])
def test_svgs_are_well_formed(name):
    root = ET.parse(asset_path(name)).getroot()
    assert root.tag.endswith("svg")
