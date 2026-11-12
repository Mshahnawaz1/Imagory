"""Takeout sidecar matching, reading, trash rule and report (PRD §8.3)."""

import json
import logging
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from imagory import takeout
from imagory.takeout import (
    DirectoryMatch,
    MatchReport,
    Sidecar,
    is_trash_dir,
    log_match_report,
    match_directory,
    parse_sidecar,
    read_sidecar,
)

LONG = "a_very_long_original_filename_from_a_camera_app_2019.jpg"


def m(*names, titles=None):
    reader = (lambda n: titles.get(n)) if titles is not None else None
    return match_directory(names, reader).matches


@pytest.mark.parametrize(
    ("image", "sidecar"),
    [
        ("IMG_1234.jpg", "IMG_1234.jpg.json"),
        ("IMG_1234.jpg", "IMG_1234.jpg.supplemental-metadata.json"),
        ("IMG_1234.jpg", "IMG_1234.jpg.supplemental-metad.json"),
        ("IMG_1234.jpg", "IMG_1234.jpg.supplemental-m.json"),
        ("IMG_1234.JPG", "img_1234.jpg.json"),
        ("IMG_1234(1).jpg", "IMG_1234.jpg(1).json"),
        ("IMG_1234(2).jpg", "IMG_1234.jpg.supplemental-metadata(2).json"),
        ("IMG_1234(1).jpg", "IMG_1234(1).jpg.json"),  # counter not moved
        (LONG, LONG[:46] + ".json"),
        (LONG, (LONG + ".supplemental-metadata")[:46] + ".json"),
    ],
)
def test_each_quirk(image, sidecar):
    assert m(image, sidecar) == {image: sidecar}


def test_numbered_counter_must_match():
    names = ["IMG_1.jpg", "IMG_1(1).jpg", "IMG_1.jpg.json", "IMG_1.jpg(1).json"]
    assert m(*names) == {"IMG_1.jpg": "IMG_1.jpg.json", "IMG_1(1).jpg": "IMG_1.jpg(1).json"}
    # the counted image does not take the uncounted sidecar
    assert m("IMG_1(1).jpg", "IMG_1.jpg.json") == {"IMG_1(1).jpg": None}


def test_edited_uses_original_sidecar_without_claiming_it():
    result = m("IMG_1.jpg", "IMG_1-edited.jpg", "IMG_1.jpg.json")
    assert result == {"IMG_1.jpg": "IMG_1.jpg.json", "IMG_1-edited.jpg": "IMG_1.jpg.json"}


def test_edited_without_original_image():
    assert m("IMG_1-edited.jpg", "IMG_1.jpg.supplemental-metadata.json") == {
        "IMG_1-edited.jpg": "IMG_1.jpg.supplemental-metadata.json"
    }


def test_edited_counter_and_localized_suffix():
    assert m("IMG_1-edited(1).jpg", "IMG_1.jpg(1).json") == {
        "IMG_1-edited(1).jpg": "IMG_1.jpg(1).json"
    }
    assert m("IMG_1-bearbeitet.jpg", "IMG_1.jpg.json") == {"IMG_1-bearbeitet.jpg": "IMG_1.jpg.json"}


def test_edited_prefers_its_own_sidecar():
    result = m("IMG_1.jpg", "IMG_1-edited.jpg", "IMG_1.jpg.json", "IMG_1-edited.jpg.json")
    assert result["IMG_1-edited.jpg"] == "IMG_1-edited.jpg.json"
    assert result["IMG_1.jpg"] == "IMG_1.jpg.json"


def test_sidecar_claimed_by_one_non_edited_image():
    # two images share a 46-character prefix; one truncated sidecar can serve only one
    a = LONG.replace(".jpg", "_a.jpg")
    b = LONG.replace(".jpg", "_b.jpg")
    side = a[:46] + ".json"
    assert a[:46] == b[:46]
    result = m(a, b, side)
    assert sorted(v for v in result.values() if v) == [side]
    assert list(result.values()).count(None) == 1


def test_title_breaks_truncation_tie():
    a = LONG.replace(".jpg", "_a.jpg")
    b = LONG.replace(".jpg", "_b.jpg")
    side = a[:46] + ".json"
    # alphabetical order would give it to a; the title says b
    assert m(a, b, side, titles={side: b}) == {a: None, b: side}
    assert m(a, b, side, titles={side: a.upper()}) == {a: side, b: None}


def test_title_mismatch_rejects_truncated_match():
    side = LONG[:46] + ".json"
    assert m(LONG, side, titles={side: "something_else_entirely.jpg"}) == {LONG: None}


def test_title_mismatch_keeps_exact_name_match():
    assert m("IMG_1.jpg", "IMG_1.jpg.json", titles={"IMG_1.jpg.json": "renamed.jpg"}) == {
        "IMG_1.jpg": "IMG_1.jpg.json"
    }


def test_title_called_lazily():
    calls = []
    match_directory(["IMG_1.jpg", "IMG_1.jpg.json"], lambda n: calls.append(n))
    assert calls == []


def test_broken_title_reader_does_not_stop_matching():
    def boom(name):
        raise RuntimeError("bad")

    assert match_directory([LONG, LONG[:46] + ".json"], boom).matched == 1


def test_non_sidecar_json_and_non_images_ignored():
    result = match_directory(["a.jpg", "metadata.json", "notes.txt", "clip.mp4", "b.json"])
    assert result.matches == {"a.jpg": None}
    assert result.unused_sidecars == ("b.json",)


def test_short_prefix_is_not_a_match():
    assert m("IMG_1234.jpg", "IMG_123.jpg.json", "IMG_12.json") == {"IMG_1234.jpg": None}


def test_deterministic_regardless_of_order():
    names = ["IMG_1.jpg", "IMG_1-edited.jpg", "IMG_1.jpg.json", "IMG_2(1).jpg", "IMG_2.jpg(1).json"]
    assert m(*names) == m(*reversed(names))


def test_match_is_case_insensitive_and_lists_unused():
    result = match_directory(["A.jpg", "a.jpg.json", "x.json"])
    assert result.matches == {"A.jpg": "a.jpg.json"}
    assert result.unused_sidecars == ("x.json",)
    assert result.unmatched_images == []
    assert isinstance(result, DirectoryMatch)


# --- reading ------------------------------------------------------------------------------


def sidecar_data(**over):
    data = {
        "title": "IMG_1.jpg",
        "photoTakenTime": {"timestamp": "1548979199", "formatted": "x"},
        "geoData": {"latitude": 48.85, "longitude": 2.35, "altitude": 1.0},
        "geoDataExif": {"latitude": 1.0, "longitude": 2.0},
    }
    data.update(over)
    return data


def test_parse_sidecar():
    sc = parse_sidecar(sidecar_data())
    assert sc == Sidecar("IMG_1.jpg", 1548979199, "2019-01-31T23:59:59Z", 48.85, 2.35)
    assert pickle.loads(pickle.dumps(sc)) == sc


def test_geo_zero_is_none_and_exif_geo_is_not_used():
    sc = parse_sidecar(sidecar_data(geoData={"latitude": 0.0, "longitude": 0.0}))
    assert (sc.latitude, sc.longitude) == (None, None)


def test_one_zero_coordinate_is_kept():
    sc = parse_sidecar(sidecar_data(geoData={"latitude": 0.0, "longitude": 12.5}))
    assert (sc.latitude, sc.longitude) == (0.0, 12.5)


@pytest.mark.parametrize(
    "geo",
    [None, "x", {}, {"latitude": "a", "longitude": 1}, {"latitude": 95, "longitude": 1}],
)
def test_bad_geo(geo):
    assert parse_sidecar(sidecar_data(geoData=geo)).latitude is None


@pytest.mark.parametrize("taken", [None, {}, {"timestamp": "abc"}, {"timestamp": "0"}, 5])
def test_bad_taken_time(taken):
    sc = parse_sidecar(sidecar_data(photoTakenTime=taken))
    assert sc.taken_timestamp is None
    assert sc.taken_utc is None


def test_huge_timestamp_is_none():
    assert parse_sidecar(sidecar_data(photoTakenTime={"timestamp": "9" * 30})).taken_utc is None


def test_parse_non_object():
    assert parse_sidecar([1]) is None


def test_read_sidecar(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps(sidecar_data()), encoding="utf-8")
    before = path.stat().st_mtime_ns
    assert read_sidecar(path).taken_utc == "2019-01-31T23:59:59Z"
    assert path.stat().st_mtime_ns == before


def test_read_sidecar_bad_files_return_none(tmp_path, caplog):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    binary = tmp_path / "bin.json"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    big = tmp_path / "big.json"
    big.write_text("[" + "0," * takeout.MAX_SIDECAR_BYTES + "0]")
    with caplog.at_level(logging.WARNING):
        assert read_sidecar(bad) is None
        assert read_sidecar(binary) is None
        assert read_sidecar(big) is None
        assert read_sidecar(tmp_path / "missing.json") is None
    assert len(caplog.records) == 4


# --- trash --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Trash", True),
        ("trash", True),
        ("Google Photos/Trash", True),
        ("Takeout/Google Photos/Trash", True),
        ("Takeout\\Google Photos\\Trash", True),
        ("Takeout/Google Fotos/Papierkorb", True),
        ("Google Photos/Archive", False),
        ("Google Photos/Trash/sub", False),
        ("Google Photos/Family/Trash", False),
        ("Photos from 2019", False),
        ("", False),
    ],
)
def test_is_trash_dir(path, expected):
    assert is_trash_dir(path) is expected


# --- report -------------------------------------------------------------------------------


def test_match_report_and_log(caplog):
    a = match_directory(["a.jpg", "a.jpg.json", "b.jpg", "x.json"])
    b = match_directory(["c.jpg"])
    report = MatchReport()
    report.add("Photos from 2019", a)
    report.add("", b)
    assert report.matched == 1
    assert report.unmatched_images == ["Photos from 2019/b.jpg", "c.jpg"]
    assert report.unused_sidecars == ["Photos from 2019/x.json"]
    with caplog.at_level(logging.INFO, logger="imagory.takeout"):
        log_match_report(report)
    assert "1 matched, 2 images without a sidecar, 1 unused sidecars" in caplog.text
    assert "c.jpg" in caplog.text


def test_log_report_caps_sample(caplog):
    report = MatchReport(unmatched_images=[f"i{n:03d}.jpg" for n in range(50)])
    with caplog.at_level(logging.INFO, logger="imagory.takeout"):
        log_match_report(report, sample=5)
    assert "(and 45 more)" in caplog.text
    assert "i049.jpg" not in caplog.text


# --- the fake Takeout export against its manifest -------------------------------------------


@pytest.fixture(scope="module")
def export(tmp_path_factory):
    out = tmp_path_factory.mktemp("takeout")
    script = Path(__file__).resolve().parents[2] / "scripts" / "make_fake_library.py"
    args = [sys.executable, str(script), str(out), "--takeout", "--count", "300", "--seed", "7"]
    subprocess.run(args, check=True, capture_output=True)
    return out, json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def match_export(out, with_titles):
    """Match every folder of the export, as the scan will."""
    results = {}
    for folder in sorted({p.parent for p in (out / "Takeout").rglob("*") if p.is_file()}):
        names = [p.name for p in folder.iterdir() if p.is_file()]
        reader = (lambda n, f=folder: takeout.read_title(f / n)) if with_titles else None
        results[folder.relative_to(out).as_posix()] = match_directory(names, reader)
    return results


@pytest.mark.parametrize("with_titles", [False, True])
def test_matching_against_manifest(export, with_titles):
    out, manifest = export
    results = match_export(out, with_titles)
    got = {
        f"{d}/{n}": (f"{d}/{s}" if s else None)
        for d, r in results.items()
        for n, s in r.matches.items()
    }
    expected = {rel: e["sidecar"] for rel, e in manifest["images"].items()}
    assert got == expected
    assert any(e["quirk"] == "supplemental_truncated" for e in manifest["images"].values())
    assert any(e["edited_from"] for e in manifest["images"].values())

    report = MatchReport()
    for relative_dir, result in results.items():
        report.add(relative_dir, result)
    assert sorted(report.unused_sidecars) == manifest["orphan_sidecars"]


def test_parsed_sidecars_match_manifest(export):
    out, manifest = export
    checked = 0
    for entry in manifest["images"].values():
        if not entry["sidecar"] or entry["edited_from"]:
            continue
        sc = read_sidecar(out / entry["sidecar"])
        assert sc is not None
        assert sc.taken_utc == entry["json_time_utc"]
        geo = entry["expected"]["geo"]
        if geo is None:
            assert sc.latitude is None
        else:
            assert (sc.latitude, sc.longitude) == (geo["latitude"], geo["longitude"])
        checked += 1
    assert checked > 100
