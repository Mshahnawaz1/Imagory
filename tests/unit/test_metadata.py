import os
import subprocess
import sys
import textwrap

from PIL import Image

from imagory import metadata, thumbs
from tests.imaging import make_image


def test_parse_exif_datetime():
    assert metadata.parse_exif_datetime("2019:01:31 23:59:59") == "2019-01-31T23:59:59"
    assert metadata.parse_exif_datetime(b"2019:01:31 23:59:59\x00") == "2019-01-31T23:59:59"
    assert metadata.parse_exif_datetime("0000:00:00 00:00:00") is None
    assert metadata.parse_exif_datetime("    :  :     :  :  ") is None
    assert metadata.parse_exif_datetime(None) is None


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
    assert result.content_hash == thumbs.hash_bytes(img.read_bytes())
    assert len(result.content_hash) == 32  # 128-bit BLAKE2b
    thumb = thumbs.thumb_path(tmp_path / "thumbs", result.content_hash)
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
    with Image.open(thumbs.thumb_path(tmp_path / "thumbs", result.content_hash)) as t:
        assert t.size == (100, 50)  # never upscaled


def test_gif_and_heic(tmp_path):
    src = tmp_path / "src"
    for name in ("c.gif", "d.heic"):
        img = make_image(src / name, (40, 30))
        result = thumbs.process_file(str(src), str(tmp_path / "thumbs"), _job(img, src))
        assert result.error is None, result.error
        assert (result.width, result.height) == (40, 30)


def test_heic_is_decoded_without_pillow_heif(tmp_path):
    """The app reads HEIC with pi-heif alone; pillow-heif (GPLv2 x265) is dev-only (PRD §13)."""
    src = tmp_path / "src"
    make_image(src / "e.heic", (40, 30))  # written with pillow-heif, the dev-only encoder
    code = textwrap.dedent(
        f"""
        import sys
        from imagory import thumbs
        job = thumbs.FileJob("e.heic", 0, 0)
        result = thumbs.process_file({str(src)!r}, {str(tmp_path / "thumbs")!r}, job)
        assert result.error is None, result.error
        assert (result.width, result.height) == (40, 30)
        assert "pi_heif" in sys.modules
        assert "pillow_heif" not in sys.modules
        """
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=os.getcwd())


def test_corrupt_file_returns_error(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    bad = src / "bad.jpg"
    bad.write_bytes(b"not really a jpeg")
    result = thumbs.process_file(str(src), str(tmp_path / "thumbs"), _job(bad, src))
    assert result.error
    assert not (tmp_path / "thumbs").exists()


def test_thumb_path_layout_is_keyed_by_content_hash(tmp_path):
    key = thumbs.hash_bytes(b"photo bytes")
    assert thumbs.thumb_path(tmp_path, key) == tmp_path / key[:2] / key[2:4] / f"{key}.jpg"
    assert thumbs.hash_bytes(b"photo bytes") == key != thumbs.hash_bytes(b"other bytes")


def test_existing_thumbnail_of_a_copy_is_reused(tmp_path):
    src = tmp_path / "src"
    a = make_image(src / "a.jpg", (500, 500))
    (src / "copy.jpg").write_bytes(a.read_bytes())
    cache = tmp_path / "thumbs"
    first = thumbs.process_file(str(src), str(cache), _job(a, src))
    thumb = thumbs.thumb_path(cache, first.content_hash)
    thumb.write_bytes(b"marker")  # would be overwritten if regenerated
    second = thumbs.process_file(str(src), str(cache), _job(src / "copy.jpg", src))
    assert second.content_hash == first.content_hash and second.error is None
    assert thumb.read_bytes() == b"marker"


def test_process_file_dates_from_filename_and_exif_offset(tmp_path):
    src = tmp_path / "src"
    named = make_image(src / "IMG_20190131_235959.jpg")
    result = thumbs.process_file(str(src), str(tmp_path / "thumbs"), _job(named, src))
    assert (result.taken_at_local, result.taken_at_utc, result.date_source) == (
        "2019-01-31T23:59:59",
        None,
        "filename",
    )
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "2020:06:01 12:00:00"
    exif.get_ifd(0x8769)[0x9011] = "+02:00"
    Image.new("RGB", (20, 20)).save(src / "zoned.jpg", exif=exif.tobytes())
    result = thumbs.process_file(str(src), str(tmp_path / "thumbs"), _job(src / "zoned.jpg", src))
    assert (result.taken_at_local, result.taken_at_utc, result.date_source) == (
        "2020-06-01T12:00:00",
        "2020-06-01T10:00:00Z",
        "exif",
    )
    assert (result.exif_taken_at, result.exif_offset) == ("2020-06-01T12:00:00", "+02:00")


def test_ensure_thumbnail_regenerates(tmp_path):
    src = tmp_path / "src"
    make_image(src / "a.jpg", (500, 500))
    cache = tmp_path / "thumbs"
    key = thumbs.hash_bytes((src / "a.jpg").read_bytes())
    first = thumbs.ensure_thumbnail(src, cache, "a.jpg", key)
    assert first == thumbs.thumb_path(cache, key) and first.exists()
    thumbs.clear_thumbs(cache)
    assert not first.exists()
    again = thumbs.ensure_thumbnail(src, cache, "a.jpg", key)
    assert again == first and again.exists()
    # A row stored without a hash: the file is hashed to find its thumbnail.
    thumbs.clear_thumbs(cache)
    assert thumbs.ensure_thumbnail(src, cache, "a.jpg", None) == first and first.exists()


def test_fit_within():
    assert thumbs.fit_within((640, 480), 320, 320) == (320, 240)
    assert thumbs.fit_within((3000, 4000), 320, 320) == (240, 320)
    assert thumbs.fit_within((100, 50), 320, 320) == (100, 50)


def test_large_jpeg_thumbnail_uses_draft(tmp_path):
    src = tmp_path / "src"
    img = make_image(src / "big.jpg", (4000, 3000))
    result = thumbs.process_file(str(src), str(tmp_path / "thumbs"), _job(img, src))
    assert (result.width, result.height) == (4000, 3000)  # original size, not draft size
    with Image.open(thumbs.thumb_path(tmp_path / "thumbs", result.content_hash)) as t:
        assert t.size == (320, 240)
