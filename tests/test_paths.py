import sys
from pathlib import Path

from imagory import paths


def test_env_override(library):
    assert paths.library_dir() == library.resolve()


def test_ensure_creates_subfolders(library):
    root = paths.ensure_library_dir()
    assert (root / "thumbs").is_dir()
    assert (root / "logs").is_dir()


def test_xdg_default(monkeypatch, tmp_path):
    monkeypatch.delenv("IMAGORY_LIBRARY")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.library_dir() == tmp_path / "xdg" / "imagory"


def test_windows_default(monkeypatch, tmp_path):
    monkeypatch.delenv("IMAGORY_LIBRARY")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.library_dir() == tmp_path / "local" / "Imagory"


def test_resource_path_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.resource_path("icons/a.png") == Path(tmp_path) / "icons/a.png"


def test_cleanup_path_helpers_exact_results(tmp_path):
    out = tmp_path / "out"
    assert paths.cleanup_dir(out) == out / ".imagory"
    assert paths.cleanup_db_path(out) == out / ".imagory" / "imagory.db"
    assert paths.cleanup_thumbs_dir(out) == out / ".imagory" / "thumbs"
    assert paths.cleanup_reports_dir(out) == out / ".imagory" / "reports"
    assert paths.cleanup_json_dir(out) == out / ".imagory" / "takeout-json"
    assert paths.metadata_json_path(out) == out / ".imagory" / "metadata.json"


def test_cleanup_path_helpers_create_nothing(tmp_path):
    out = tmp_path / "out"
    paths.cleanup_dir(out)
    paths.cleanup_db_path(out)
    paths.cleanup_thumbs_dir(out)
    paths.cleanup_reports_dir(out)
    paths.cleanup_json_dir(out)
    paths.metadata_json_path(out)
    assert not out.exists()
