from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def library(tmp_path, monkeypatch):
    """Every test gets its own library folder, so the real one is never touched."""
    lib = tmp_path / "library"
    monkeypatch.setenv("IMAGORY_LIBRARY", str(lib))
    return lib
