"""Takeout type detection (PRD §8.1): folder names and the sidecar heuristic."""

import pytest

from imagory import takeout
from imagory.takeout import SidecarNames


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@pytest.mark.parametrize("layout", ["Google Photos", "Takeout/Google Photos", "Google Fotos"])
def test_google_photos_folder_name(tmp_path, layout):
    (tmp_path / layout).mkdir(parents=True)
    root = tmp_path / layout.split("/")[0]
    assert takeout.detect_source_type(root).type == "takeout"


def test_google_photos_name_must_be_a_folder(tmp_path):
    touch(tmp_path / "src" / "Google Photos")
    assert takeout.detect_source_type(tmp_path / "src").type == "plain"


def test_plain_folder(tmp_path):
    touch(tmp_path / "a.jpg")
    touch(tmp_path / "sub" / "b.png")
    touch(tmp_path / "notes.json")
    detection = takeout.detect_source_type(tmp_path)
    assert detection.type == "plain"
    assert detection.reason


def test_sidecars_next_to_most_images(tmp_path):
    year = tmp_path / "Photos from 2019"
    for i in range(3):
        touch(year / f"IMG_{i}.jpg")
        touch(year / f"IMG_{i}.jpg.supplemental-metadata.json")
    touch(year / "IMG_9.jpg")  # 3 of 4 have a sidecar
    detection = takeout.detect_source_type(tmp_path)
    assert detection.type == "takeout"
    assert "75%" in detection.reason


def test_too_few_sidecars_is_plain(tmp_path):
    touch(tmp_path / "a.jpg.json")
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        touch(tmp_path / name)
    assert takeout.detect_source_type(tmp_path).type == "plain"


def test_hidden_folders_are_not_sampled(tmp_path):
    touch(tmp_path / "a.jpg")
    for i in range(5):
        touch(tmp_path / ".cache" / f"{i}.jpg")
        touch(tmp_path / ".cache" / f"{i}.jpg.json")
    assert takeout.detect_source_type(tmp_path).type == "plain"


def test_unreadable_folder_is_plain(tmp_path):
    assert takeout.detect_source_type(tmp_path / "nope").type == "plain"


LONG = "a_very_long_original_filename_from_a_camera_app_2019.jpg"


@pytest.mark.parametrize(
    ("image", "sidecar"),
    [
        ("IMG_1234.jpg", "IMG_1234.jpg.json"),
        ("IMG_1234.jpg", "IMG_1234.jpg.supplemental-metadata.json"),
        ("IMG_1234.jpg", "IMG_1234.jpg.supplemental-metad.json"),
        ("IMG_1234.JPG", "img_1234.jpg.json"),
        ("IMG_1234(1).jpg", "IMG_1234.jpg(1).json"),
        ("IMG_1234-edited.jpg", "IMG_1234.jpg.json"),
        (LONG, LONG[:46] + ".json"),
    ],
)
def test_sidecar_name_forms(image, sidecar):
    assert SidecarNames([sidecar]).has_sidecar(image)


@pytest.mark.parametrize(
    ("image", "sidecar"),
    [
        ("IMG_1234.jpg", "IMG_123.jpg.json"),
        ("IMG_1234.jpg", "IMG_12.json"),  # short truncations are too loose to trust
        ("IMG_1234.jpg", "metadata.json"),
    ],
)
def test_not_a_sidecar(image, sidecar):
    assert not SidecarNames([sidecar]).has_sidecar(image)
