"""Tests for the cleanup metadata.json and report writers (imagory/cleanup_export.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imagory import cleanup_export
from imagory.cleanup_export import write_metadata_json, write_report
from imagory.cleanup_types import (
    CleanupReport,
    ExportAlbum,
    ExportFile,
    ExportOrigin,
    ExportRun,
    Rename,
)

SIDECAR = "Takeout/Google Photos/Photos from 2019/IMG_1234.jpg.supplemental-metadata.json"
NAMES = [
    "say \"hi\" 'there'",
    "line one\nline two",
    "back\\slash\\path",
    "photo 😀 🎉.jpg",
    "tab\there.jpg",
]


def _photo(path: str = "2019/07/IMG_1234.jpg", **overrides) -> ExportFile:
    values = dict(
        path=path,
        kind="photo",
        size=123456,
        content_hash="0123456789abcdef0123456789abcdef",
        width=4032,
        height=3024,
        format="JPEG",
        taken_at_local="2019-07-04T12:00:00",
        taken_at_utc="2019-07-04T10:00:00Z",
        date_source="takeout_json",
        latitude=48.85,
        longitude=2.35,
        people=("Ana",),
        description="",
        favorite=False,
        uploaded_at_utc="2019-07-05T08:00:00Z",
        origins=(
            ExportOrigin(
                run=1,
                path="Takeout/Google Photos/Photos from 2019/IMG_1234.jpg",
                sidecar=SIDECAR,
                album=None,
            ),
        ),
    )
    values.update(overrides)
    return ExportFile(**values)


def _other(path: str = "Unknown date/clip.mp4", **overrides) -> ExportFile:
    values = dict(
        path=path,
        kind="other",
        size=999,
        content_hash="fedcba9876543210fedcba9876543210",
    )
    values.update(overrides)
    return ExportFile(**values)


def _run() -> ExportRun:
    return ExportRun(
        id=1,
        input_path="D:/Takeout",
        started_at="2026-11-07T10:00:00Z",
        finished_at="2026-11-07T10:05:00Z",
        counts={"photos": 2, "other": 1},
    )


def _album() -> ExportAlbum:
    return ExportAlbum(name="Trip 2019", files=("2019/07/IMG_1234.jpg",))


def _write_meta(path: Path, files: list[ExportFile]) -> dict:
    write_metadata_json(
        path,
        [_run()],
        [_album()],
        files,
        generated_at="2026-11-07T10:00:00Z",
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _empty_report(**overrides) -> CleanupReport:
    values = dict(
        counts={},
        trash_skipped=(),
        duplicates=(),
        unmatched_sidecars=(),
        unreadable=(),
        renames=(),
        unaccounted=(),
    )
    values.update(overrides)
    return CleanupReport(**values)


# --- metadata.json ---------------------------------------------------------------------------


def test_metadata_round_trip_top_level(tmp_path: Path) -> None:
    data = _write_meta(tmp_path / "metadata.json", [_photo(), _other()])

    assert data["format"] == "imagory-takeout-cleanup"
    assert data["version"] == 1
    assert data["generated_at"] == "2026-11-07T10:00:00Z"
    assert data["runs"] == [
        {
            "id": 1,
            "input_path": "D:/Takeout",
            "started_at": "2026-11-07T10:00:00Z",
            "finished_at": "2026-11-07T10:05:00Z",
            "counts": {"photos": 2, "other": 1},
        }
    ]
    assert data["albums"] == [{"name": "Trip 2019", "files": ["2019/07/IMG_1234.jpg"]}]
    assert len(data["files"]) == 2


def test_metadata_top_level_key_order(tmp_path: Path) -> None:
    write_metadata_json(tmp_path / "metadata.json", [], [], [], "2026-11-07T10:00:00Z")
    data = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))

    assert list(data) == ["format", "version", "generated_at", "runs", "albums", "files"]


def test_metadata_photo_key_order_and_values(tmp_path: Path) -> None:
    data = _write_meta(tmp_path / "metadata.json", [_photo()])
    photo = data["files"][0]

    assert list(photo) == [
        "path",
        "kind",
        "size",
        "content_hash",
        "width",
        "height",
        "format",
        "taken_at_local",
        "taken_at_utc",
        "date_source",
        "latitude",
        "longitude",
        "people",
        "description",
        "favorite",
        "uploaded_at_utc",
        "origins",
    ]
    assert photo["kind"] == "photo"
    assert photo["width"] == 4032
    assert photo["format"] == "JPEG"
    assert photo["people"] == ["Ana"]
    assert photo["origins"] == [
        {
            "run": 1,
            "path": "Takeout/Google Photos/Photos from 2019/IMG_1234.jpg",
            "sidecar": SIDECAR,
            "album": None,
        }
    ]


def test_metadata_other_file_omits_photo_only_fields(tmp_path: Path) -> None:
    data = _write_meta(tmp_path / "metadata.json", [_other()])
    other = data["files"][0]

    assert "width" not in other
    assert "height" not in other
    assert "format" not in other
    assert other["kind"] == "other"
    assert other["taken_at_local"] is None
    assert other["taken_at_utc"] is None
    assert other["date_source"] is None
    assert other["latitude"] is None
    assert other["people"] == []
    assert other["origins"] == []
    assert list(other) == [
        "path",
        "kind",
        "size",
        "content_hash",
        "taken_at_local",
        "taken_at_utc",
        "date_source",
        "latitude",
        "longitude",
        "people",
        "description",
        "favorite",
        "uploaded_at_utc",
        "origins",
    ]


def test_metadata_other_file_keeps_values_if_set(tmp_path: Path) -> None:
    # Even if a caller sets width on an "other" file, the rule is that it is omitted.
    data = _write_meta(tmp_path / "metadata.json", [_other(width=10, height=20, format="MP4")])

    assert "width" not in data["files"][0]
    assert "height" not in data["files"][0]
    assert "format" not in data["files"][0]


def test_metadata_undated_photo_writes_nulls(tmp_path: Path) -> None:
    data = _write_meta(
        tmp_path / "metadata.json",
        [_photo(taken_at_local=None, taken_at_utc=None, date_source=None)],
    )
    photo = data["files"][0]

    assert photo["taken_at_local"] is None
    assert photo["taken_at_utc"] is None
    assert photo["date_source"] is None


def test_metadata_names_with_special_characters_survive(tmp_path: Path) -> None:
    files = [_photo(path=f"2019/07/{name}") for name in NAMES]
    files += [
        _photo(
            path=f"2019/07/x{index}.jpg",
            people=(name,),
            description=name,
            origins=(ExportOrigin(run=1, path=name, sidecar=name, album=name),),
        )
        for index, name in enumerate(NAMES)
    ]
    albums = [ExportAlbum(name=name, files=(f"2019/07/{name}",)) for name in NAMES]

    path = tmp_path / "metadata.json"
    write_metadata_json(path, [_run()], albums, files, "2026-11-07T10:00:00Z")
    data = json.loads(path.read_text(encoding="utf-8"))

    assert [item["path"] for item in data["files"][: len(NAMES)]] == [
        f"2019/07/{name}" for name in NAMES
    ]
    for index, name in enumerate(NAMES):
        item = data["files"][len(NAMES) + index]
        assert item["people"] == [name]
        assert item["description"] == name
        assert item["origins"][0]["path"] == name
        assert item["origins"][0]["sidecar"] == name
        assert item["origins"][0]["album"] == name
    assert [album["name"] for album in data["albums"]] == NAMES


def test_metadata_is_utf8_without_ascii_escapes(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    write_metadata_json(path, [], [], [_photo(path="photo 😀.jpg")], "2026-11-07T10:00:00Z")

    raw = path.read_bytes().decode("utf-8")
    assert "😀" in raw


def test_metadata_no_tmp_left_after_success(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    write_metadata_json(path, [_run()], [_album()], [_photo()], "2026-11-07T10:00:00Z")

    assert path.exists()
    assert not (tmp_path / "metadata.json.tmp").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["metadata.json"]


def test_metadata_replaces_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    path.write_text("old content that is not json", encoding="utf-8")

    data = _write_meta(path, [_photo()])

    assert data["format"] == "imagory-takeout-cleanup"
    assert len(data["files"]) == 1
    assert not (tmp_path / "metadata.json.tmp").exists()


def test_metadata_failure_removes_tmp_and_keeps_old_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "metadata.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(cleanup_export.os, "replace", broken_replace)

    with pytest.raises(OSError, match="disk gone"):
        write_metadata_json(path, [], [], [_photo()], "2026-11-07T10:00:00Z")

    assert not (tmp_path / "metadata.json.tmp").exists()
    assert path.read_text(encoding="utf-8") == '{"old": true}'


def test_metadata_write_failure_removes_only_tmp(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    # A lone surrogate cannot be encoded as UTF-8, so the write itself fails midway.
    bad_file = _photo(path="x.jpg", description="bad \ud800 text")
    sibling = tmp_path / "keep.txt"
    sibling.write_text("keep me", encoding="utf-8")

    with pytest.raises(UnicodeEncodeError):
        write_metadata_json(path, [], [], [bad_file], "2026-11-07T10:00:00Z")

    assert not path.exists()
    assert not (tmp_path / "metadata.json.tmp").exists()
    assert sibling.read_text(encoding="utf-8") == "keep me"


# --- report ----------------------------------------------------------------------------------

HEADINGS = [
    "Counts",
    "Trash skipped",
    "Duplicates",
    "Unmatched sidecars",
    "Unreadable files",
    "Renames",
    "Not accounted for",
]


def _report_text(tmp_path: Path, report: CleanupReport) -> str:
    path = tmp_path / "cleanup.txt"
    write_report(path, report)
    return path.read_text(encoding="utf-8")


def _section_lines(text: str, heading: str) -> list[str]:
    lines = text.split("\n")
    start = lines.index(heading)
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line in HEADINGS:
            break
        body.append(line)
    # Drop the blank separator line before the next heading (and trailing empties).
    while body and body[-1] == "":
        body.pop()
    return body


def test_report_headings_present_in_order(tmp_path: Path) -> None:
    lines = _report_text(tmp_path, _empty_report()).split("\n")

    positions = [lines.index(heading) for heading in HEADINGS]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(HEADINGS)


def test_report_empty_lists_print_none(tmp_path: Path) -> None:
    text = _report_text(tmp_path, _empty_report())

    for heading in HEADINGS:
        assert _section_lines(text, heading) == ["(none)"], heading


def test_report_counts_in_mapping_order(tmp_path: Path) -> None:
    report = _empty_report(counts={"photos": 3, "other": 1, "duplicates": 2})
    text = _report_text(tmp_path, report)

    assert _section_lines(text, "Counts") == ["photos: 3", "other: 1", "duplicates: 2"]


def test_report_lines_for_each_list(tmp_path: Path) -> None:
    report = _empty_report(
        trash_skipped=("Trash/a.jpg", "Trash/b.jpg"),
        duplicates=(("2019/07/IMG_1.jpg", "Takeout/a/IMG_1.jpg"),),
        unmatched_sidecars=("Takeout/x.jpg.json",),
        unreadable=("Takeout/corrupt.jpg",),
        renames=(Rename(original="IMG_1.jpg", target="2019/07/IMG_1 (1).jpg", reason="clash"),),
        unaccounted=("Takeout/left.bin",),
    )
    text = _report_text(tmp_path, report)

    assert _section_lines(text, "Trash skipped") == ["Trash/a.jpg", "Trash/b.jpg"]
    assert _section_lines(text, "Duplicates") == ["2019/07/IMG_1.jpg  <-  Takeout/a/IMG_1.jpg"]
    assert _section_lines(text, "Unmatched sidecars") == ["Takeout/x.jpg.json"]
    assert _section_lines(text, "Unreadable files") == ["Takeout/corrupt.jpg"]
    assert _section_lines(text, "Renames") == ["IMG_1.jpg  ->  2019/07/IMG_1 (1).jpg  (clash)"]
    assert _section_lines(text, "Not accounted for") == ["Takeout/left.bin"]


def test_report_names_with_special_characters_survive(tmp_path: Path) -> None:
    names = [name for name in NAMES if "\n" not in name and "\t" not in name]
    report = _empty_report(
        trash_skipped=tuple(names),
        renames=(Rename(original=names[0], target=names[-1], reason="sanitized"),),
    )
    text = _report_text(tmp_path, report)

    assert _section_lines(text, "Trash skipped") == names
    assert _section_lines(text, "Renames") == [f"{names[0]}  ->  {names[-1]}  (sanitized)"]


def test_report_control_characters_stay_on_one_line(tmp_path: Path) -> None:
    text = _report_text(tmp_path, _empty_report(unaccounted=("line one\nline two", "tab\there")))

    assert _section_lines(text, "Not accounted for") == ["line one\\nline two", "tab\\there"]


def test_report_is_utf8_with_emoji(tmp_path: Path) -> None:
    path = tmp_path / "cleanup.txt"
    write_report(path, _empty_report(unreadable=("photo 😀.jpg",)))

    assert "photo 😀.jpg" in path.read_bytes().decode("utf-8")


def test_report_no_tmp_left_and_existing_file_replaced(tmp_path: Path) -> None:
    path = tmp_path / "cleanup.txt"
    path.write_text("previous report", encoding="utf-8")

    write_report(path, _empty_report(counts={"photos": 1}))

    text = path.read_text(encoding="utf-8")
    assert "previous report" not in text
    assert "photos: 1" in text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cleanup.txt"]


def test_report_failure_removes_tmp(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "cleanup.txt"

    def broken_replace(src, dst):
        raise OSError("locked")

    monkeypatch.setattr(cleanup_export.os, "replace", broken_replace)

    with pytest.raises(OSError, match="locked"):
        write_report(path, _empty_report())

    assert not path.exists()
    assert not (tmp_path / "cleanup.txt.tmp").exists()
