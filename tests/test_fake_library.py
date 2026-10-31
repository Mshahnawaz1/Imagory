"""The fake library generator (PRD §10), mainly its synthetic Takeout export."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_fake_library.py"
GP = "Takeout/Google Photos"


def _load_module():
    spec = importlib.util.spec_from_file_location("make_fake_library", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_module()


def _run(out: Path, *args: str) -> None:
    subprocess.run([sys.executable, str(SCRIPT), str(out), *args], check=True, capture_output=True)


def _tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(scope="module")
def takeout(tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("takeout")
    _run(out, "--takeout", "--count", "300", "--seed", "7")
    return out, json.loads((out / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("title", "style", "counter", "expected"),
    [
        ("IMG_1234.jpg", "plain", 0, "IMG_1234.jpg.json"),
        ("IMG_1234.jpg", "supplemental", 0, "IMG_1234.jpg.supplemental-metadata.json"),
        ("IMG_1234.jpg", "plain", 1, "IMG_1234.jpg(1).json"),
        ("IMG_1234.jpg", "supplemental", 2, "IMG_1234.jpg.supplemental-metadata(2).json"),
        ("Screenshot_20190131-235959.png", "supplemental", 0,
         "Screenshot_20190131-235959.png.supplemental-me.json"),
        ("PXL_20190131_235959123.jpg", "supplemental", 0,
         "PXL_20190131_235959123.jpg.supplemental-metada.json"),
        ("IMG_20190131_235959_123.jpg", "supplemental", 0,
         "IMG_20190131_235959_123.jpg.supplemental-metad.json"),
        ("Holiday in the mountains with the whole family 2019.jpg", "plain", 0,
         "Holiday in the mountains with the whole family.json"),
    ],
)  # fmt: skip
def test_sidecar_name(title, style, counter, expected):
    assert gen.sidecar_name(title, style, counter) == expected
    assert len(expected) - len(f"({counter})" if counter else "") <= 51


def test_layout(takeout):
    out, manifest = takeout
    gp = out / GP
    assert (out / "manifest.json").is_file()
    assert not list(gp.rglob("manifest.json"))
    assert list(gp.glob("Photos from 20??"))
    for folder in ("Trash", "Archive"):
        assert list((gp / folder).glob("*.jpg"))
    for meta in manifest["album_metadata"]:
        assert json.loads((out / meta).read_text(encoding="utf-8"))["title"] in meta
    kinds = {e["folder_kind"] for e in manifest["images"].values()}
    assert kinds == {"year", "album", "trash", "archive"}


def test_every_file_is_in_the_manifest(takeout):
    out, manifest = takeout
    images = manifest["images"]
    sidecars = {e["sidecar"] for e in images.values() if e["sidecar"]}
    known_json = sidecars | set(manifest["orphan_sidecars"]) | set(manifest["album_metadata"])
    known_json |= set(manifest["other_json"])
    files = set(_tree(out)) - {"manifest.json", "Takeout/archive_browser.html"}
    assert {f for f in files if not f.endswith(".json")} == set(images)
    assert {f for f in files if f.endswith(".json")} == known_json
    assert len({f.lower() for f in files}) == len(files)  # no clashes on Windows


def test_album_copies_are_exact(takeout):
    out, manifest = takeout
    copies = {p: e for p, e in manifest["images"].items() if e["folder_kind"] == "album"}
    assert copies
    for path, entry in copies.items():
        src = manifest["images"][entry["copy_of"]]
        assert (out / path).read_bytes() == (out / entry["copy_of"]).read_bytes()
        assert entry["expected"] == src["expected"]
        if entry["sidecar"]:
            assert (out / entry["sidecar"]).read_bytes() == (out / src["sidecar"]).read_bytes()


def test_sidecar_contents_match_manifest(takeout):
    out, manifest = takeout
    geo_zero = geo_real = 0
    for path, entry in manifest["images"].items():
        if not entry["sidecar"]:
            continue
        data = json.loads((out / entry["sidecar"]).read_text(encoding="utf-8"))
        assert data["title"] == entry["title"]
        if entry["quirk"] not in ("numbered", "edited"):
            assert data["title"] == path.rsplit("/", 1)[1]
        stamp = data["photoTakenTime"]["timestamp"]
        assert isinstance(stamp, str)
        utc = datetime(1970, 1, 1) + timedelta(seconds=int(stamp))
        assert f"{utc:%Y-%m-%dT%H:%M:%S}Z" == entry["json_time_utc"]
        assert data["photoTakenTime"]["formatted"].endswith(" UTC")
        assert int(data["creationTime"]["timestamp"]) > int(stamp)
        assert {"geoData", "geoDataExif"} <= data.keys()
        geo = data["geoData"]
        if geo["latitude"] == 0.0 and geo["longitude"] == 0.0:
            geo_zero += 1
            assert entry["expected"]["geo"] is None
        else:
            geo_real += 1
            assert entry["expected"]["geo"] == {k: geo[k] for k in ("latitude", "longitude")}
        if entry["folder_kind"] == "trash":
            assert data["trashed"] is True
    assert geo_zero > 20 and geo_real > 20


def test_naming_quirks(takeout):
    _, manifest = takeout
    images = manifest["images"]
    counts = manifest["counts"]
    for quirk in gen.QUIRK_WEIGHTS:
        assert counts[f"quirk:{quirk}"] >= 5, quirk
    assert counts["orphan_sidecars"] >= 2

    def side(entry):
        return entry["sidecar"].rsplit("/", 1)[1]

    for path, entry in images.items():
        name = path.rsplit("/", 1)[1]
        quirk = entry["quirk"]
        if quirk == "plain":
            assert side(entry) == f"{name}.json"
        elif quirk == "supplemental":
            assert side(entry) == f"{name}.supplemental-metadata.json"
        elif quirk == "supplemental_truncated":
            assert side(entry).startswith(f"{name}.s")
            assert ".supplemental-metadata" not in side(entry)
            assert len(side(entry)) == 51
        elif quirk == "long_name":
            assert len(name) > 46
            assert side(entry) == f"{name[:46]}.json"
        elif quirk == "numbered":
            stem, ext = name.rsplit(".", 1)
            counter = stem[stem.rindex("(") :]
            assert entry["title"] == f"{stem[: stem.rindex('(')]}.{ext}"
            assert side(entry).endswith(f"{counter}.json")
            assert side(entry).startswith(entry["title"])
        elif quirk == "edited":
            assert "-edited." in name
            original = entry["edited_from"]
            assert entry["sidecar"] == images[original]["sidecar"]
            assert original.rsplit("/", 1)[1] == name.replace("-edited", "")
        elif quirk == "no_sidecar":
            assert entry["sidecar"] is None


def test_orphan_sidecars_have_no_image(takeout):
    out, manifest = takeout
    names = {p.rsplit("/", 1)[1].lower() for p in manifest["images"]}
    for orphan in manifest["orphan_sidecars"]:
        title = json.loads((out / orphan).read_text(encoding="utf-8"))["title"]
        assert title.lower() not in names


def test_dates(takeout):
    out, manifest = takeout
    counts = manifest["counts"]
    for case in ("tz_offset", "over_24h", "no_exif"):
        assert counts[f"date:{case}"] >= 10, case
    sources = set()
    for path, entry in manifest["images"].items():
        exif = Image.open(out / path).getexif().get_ifd(0x8769).get(0x9003)
        expected_exif = entry["exif_time"]
        assert exif == (
            expected_exif.replace("-", ":").replace("T", " ") if expected_exif else None
        )
        expected = entry["expected"]
        sources.add(expected["date_source"])
        if expected_exif and entry["json_time_utc"]:
            diff = abs(
                datetime.fromisoformat(expected_exif)
                - datetime.fromisoformat(entry["json_time_utc"][:-1])
            )
            if entry["date_case"] == "tz_offset":
                assert timedelta(0) < diff < timedelta(hours=24)
                assert expected["taken_at_local"] == expected_exif
            elif entry["date_case"] == "over_24h":
                assert diff > timedelta(hours=24)
                assert expected["taken_at_local"] is None
        mtime = datetime(1970, 1, 1) + timedelta(seconds=(out / path).stat().st_mtime_ns // 10**9)
        assert f"{mtime:%Y-%m-%dT%H:%M:%S}Z" == entry["mtime_utc"]
    assert sources == {"takeout_json", "exif", "filename", "mtime"}


def test_filename_patterns(takeout):
    _, manifest = takeout
    patterns = {
        "IMG_YYYYMMDD_HHMMSS": r"IMG_\d{8}_\d{6}(_\d{3})?\.jpg",
        "PXL": r"PXL_\d{8}_\d{9}\.jpg",
        "WhatsApp": r"IMG-\d{8}-WA\d{4}\.jpg",
        "Screenshot": r"Screenshot_\d{8}-\d{6}\.png",
        "dotted": r"\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2}\.jpg",
    }
    names = {p.rsplit("/", 1)[1]: e for p, e in manifest["images"].items()}
    for label, pattern in patterns.items():
        matches = [e for n, e in names.items() if re.fullmatch(pattern, n)]
        assert len(matches) >= 3, label
        for entry in matches:
            assert entry["filename_time"]
            digits = re.sub(r"\D", "", entry["filename_time"])
            name = next(n for n, e in names.items() if e is entry)
            assert digits in re.sub(r"\D", "", name)


def test_deterministic_by_seed(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _run(a, "--takeout", "--count", "60", "--seed", "3")
    _run(b, "--takeout", "--count", "60", "--seed", "3")
    _run(c, "--takeout", "--count", "60", "--seed", "4")
    assert _tree(a) == _tree(b)
    assert _tree(a) != _tree(c)


def test_plain_mode_unchanged(tmp_path):
    out = tmp_path / "plain"
    _run(out, "--count", "40")
    files = _tree(out)
    assert not (out / "manifest.json").exists()
    assert not (out / "Takeout").exists()
    assert len([f for f in files if f.startswith("Copies/")]) == 2
    assert len([f for f in files if f.startswith("Broken/")]) == 10
    assert len([f for f in files if f.startswith("Videos/")]) == 20
    photos = [f for f in files if f[:2] in ("19", "20")]
    assert len(photos) == 38
    assert all(f.split("/")[0].isdigit() and f.split("/")[1].isdigit() for f in photos)
