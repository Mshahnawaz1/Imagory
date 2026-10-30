import os
from datetime import datetime

from PIL import Image

from imagory import metadata, thumbs
from tests.imaging import make_image


def test_parse_exif_datetime():
    assert metadata.parse_exif_datetime("2019:01:31 23:59:59") == "2019-01-31T23:59:59"
    assert metadata.parse_exif_datetime(b"2019:01:31 23:59:59\x00") == "2019-01-31T23:59:59"
    assert metadata.parse_exif_datetime("0000:00:00 00:00:00") is None
    assert metadata.parse_exif_datetime("    :  :     :  :  ") is None
    assert metadata.parse_exif_datetime(None) is None


def test_date_fallback_prefers_exif():
    assert metadata.best_date("2019-01-31T23:59:59", 0) == ("2019-01-31T23:59:59", "exif")


def test_date_fallback_uses_local_mtime():
    ns = 1_600_000_000_123_456_789
    expected = datetime.fromtimestamp(1_600_000_000).isoformat(timespec="seconds")
    assert metadata.best_date(None, ns) == (expected, "mtime")


def _job(path, root):
    st = path.stat()
    rel = path.relative_to(root).as_posix()
    return thumbs.FileJob(rel, st.st_size, st.st_mtime_ns)


def test_process_file_with_exif_and_orientation(tmp_path):
    src = tmp_path / "src"
    img = make_image(src / "a.jpg", (800, 400), taken="2019:01:31 23:59:59", orientation=6)
    result = thumbs.process_file(str(src), str(tmp_path / "thumbs"), _job(img, src))
    assert result.error is None
    assert (result.width, result.height) == (400, 800)  # rotated 90 degrees
    assert result.format == "JPEG"
    assert (result.taken_at_local, result.date_source) == ("2019-01-31T23:59:59", "exif")
    thumb = thumbs.thumb_path(tmp_path / "thumbs", "a.jpg")
    with Image.open(thumb) as t:
        assert t.size == (160, 320)  # portrait after orientation, 320 px longest edge
        assert t.format == "JPEG"


def test_process_file_without_exif_uses_mtime(tmp_path):
    src = tmp_path / "src"
    img = make_image(src / "sub" / "b.png", (100, 50))
    os.utime(img, ns=(1_500_000_000_000_000_000, 1_500_000_000_000_000_000))
    result = thumbs.process_file(str(src), str(tmp_path / "thumbs"), _job(img, src))
    assert result.error is None
    assert result.date_source == "mtime"
    assert result.taken_at_local == metadata.mtime_to_local(1_500_000_000_000_000_000)
    assert (result.width, result.height, result.format) == (100, 50, "PNG")
    with Image.open(thumbs.thumb_path(tmp_path / "thumbs", "sub/b.png")) as t:
        assert t.size == (100, 50)  # never upscaled


def test_gif_and_heic(tmp_path):
    src = tmp_path / "src"
    for name in ("c.gif", "d.heic"):
        img = make_image(src / name, (40, 30))
        result = thumbs.process_file(str(src), str(tmp_path / "thumbs"), _job(img, src))
        assert result.error is None, result.error
        assert (result.width, result.height) == (40, 30)


def test_corrupt_file_returns_error(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    bad = src / "bad.jpg"
    bad.write_bytes(b"not really a jpeg")
    result = thumbs.process_file(str(src), str(tmp_path / "thumbs"), _job(bad, src))
    assert result.error
    assert not thumbs.thumb_path(tmp_path / "thumbs", "bad.jpg").exists()


def test_thumb_path_layout_and_case_folding(tmp_path):
    p = thumbs.thumb_path(tmp_path, "Folder/IMG.JPG")
    key = p.stem
    assert p == tmp_path / key[:2] / key[2:4] / f"{key}.jpg"
    assert thumbs.thumb_path(tmp_path, "folder/img.jpg") == p


def test_ensure_thumbnail_regenerates(tmp_path):
    src = tmp_path / "src"
    make_image(src / "a.jpg", (500, 500))
    cache = tmp_path / "thumbs"
    first = thumbs.ensure_thumbnail(src, cache, "a.jpg")
    assert first and first.exists()
    thumbs.clear_thumbs(cache)
    assert not first.exists()
    again = thumbs.ensure_thumbnail(src, cache, "a.jpg")
    assert again == first and again.exists()
