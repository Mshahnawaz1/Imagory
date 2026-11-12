"""The viewer info panel's people, caption, favourite, and upload rows (PRD §8.6, §8.9)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imagory import db  # noqa: E402
from imagory.ui import info_panel  # noqa: E402


def info(**extra) -> db.PhotoInfo:
    loc = db.PhotoLocation(1, "Phone", "/x", "a.jpg", 10, "ok")
    return db.PhotoInfo(1, None, None, None, None, 1, 1, "JPEG", loc, (), **extra)


def test_format_helpers():
    assert info_panel.format_utc("2019-01-31T23:59:59Z") == "2019-01-31 23:59:59 UTC"
    assert info_panel.format_utc(None) == ""
    assert info_panel.format_people(["Anna", "", "Bo"]) == "Anna, Bo"
    assert info_panel.format_people([]) == ""


def test_rows_shown_when_present():
    text = info_panel.describe(
        info(
            people=("Anna", "Bo"),
            description="Birthday",
            favorite=True,
            uploaded_at_utc="2020-02-02T10:00:00Z",
        )
    )
    assert "People" in text and "Anna, Bo" in text
    assert "Caption" in text and "Birthday" in text
    assert "Favourite" in text
    assert "Added to Google Photos" in text and "2020-02-02 10:00:00 UTC" in text


def test_empty_rows_hidden():
    text = info_panel.describe(info())
    for label in ("People", "Caption", "Favourite", "Added to Google Photos"):
        assert label not in text


def test_names_and_captions_are_escaped():
    text = info_panel.describe(info(people=("<b>x</b>",), description="<script>a()</script>"))
    assert "<script>" not in text and "<b>x</b>" not in text
    assert "&lt;script&gt;" in text and "&lt;b&gt;x&lt;/b&gt;" in text


def test_photo_info_reads_people_from_database(tmp_path):
    conn = db.open_database(tmp_path / "imagory.db")
    sid = conn.execute(
        "INSERT INTO sources (label, root_path, added_at) VALUES ('S', '/x', 'z')"
    ).lastrowid
    pid = conn.execute("INSERT INTO photos (content_hash) VALUES ('h')").lastrowid
    fid = conn.execute(
        "INSERT INTO files (source_id, photo_id, relative_path, size, modified_time, "
        "first_seen_at, last_seen_at) VALUES (?, ?, 'a.jpg', 1, 1, 'z', 'z')",
        (sid, pid),
    ).lastrowid
    conn.execute("UPDATE photos SET primary_file_id = ? WHERE id = ?", (fid, pid))
    db.set_photo_takeout_metadata(conn, fid, ["Zed", "Amy"], "Hi", True, "2020-01-01T00:00:00Z")
    text = info_panel.describe(db.photo_info(conn, pid))
    assert "Amy, Zed" in text and "Hi" in text and "Favourite" in text
    conn.close()
