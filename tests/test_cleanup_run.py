"""Takeout cleanup pipeline (imagory/cleanup/pipeline.py) on the fake Takeout's cleanup cases.

The ground truth is ``manifest["cleanup"]`` from ``scripts/make_fake_library.py --cleanup``.
Every test that runs the pipeline asserts that the input folder is unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from imagory import dates, db, paths
from imagory.cleanup import export, planning
from imagory.cleanup.checks import (
    check_folders,
    check_input,
    input_is_source,
    storage_warning,
    unfinished_run,
)
from imagory.cleanup.inventory import album_dir_of, match_other_files
from imagory.cleanup.pipeline import CleanupPipeline
from imagory.cleanup.types import CleanupError, CleanupReport, NotEnoughSpace, Rename
from imagory.thumbs import hash_bytes
from tests.fs_snapshot import assert_unchanged, snapshot

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_fake_library.py"
GP = "Google Photos"
YEAR04 = f"{GP}/Photos from 2004"
ALBUM = f"{GP}/Cleanup album"
ALBUM_TITLE = "Lisbon trip 2004"
COUNT, SEED = 12, 3  # small, so the suite stays fast
WORKERS = 2
# Two different photos in one folder whose names differ only in case. That can't happen on
# Windows; on Linux the scan keeps whichever it meets first and the other is copied as-is
# ("other", no EXIF read), so tests that depend on which one won skip them.
CASE_CLASHES = frozenset(
    {f"Google Photos/Cleanup album/{n}" for n in ("case_pair.JPG", "Case_Pair.jpg")}
)


def _generate(out: Path, *args: str) -> dict:
    """Run the generator as a script (its process pool needs an importable main module)."""
    subprocess.run([sys.executable, str(SCRIPT), str(out), *args], check=True, capture_output=True)
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def make_input(out: Path, seed: int = SEED) -> tuple[Path, dict]:
    """A fake Takeout with the cleanup cases; returns the input root and the ground truth
    with paths relative to that root (the manifest's are relative to ``out``)."""
    manifest = _generate(
        out, "--takeout", "--cleanup", "--count", str(COUNT), "--duplicates", "0.2",
        "--seed", str(seed),
    )  # fmt: skip
    return out / "Takeout", _truth(manifest["cleanup"])


def _truth(block: dict) -> dict:
    strip = len(block["source_root"]) + 1
    files = {}
    for rel, e in block["files"].items():
        e = dict(e)
        for key in ("sidecar", "companion_of", "edited_of", "sidecar_of"):
            if e.get(key):
                e[key] = e[key][strip:]
        files[rel[strip:]] = e
    return {**block, "files": files}


def pipeline(inp: Path, out: Path, **kw) -> CleanupPipeline:
    kw.setdefault("workers", WORKERS)
    return CleanupPipeline(inp, out, **kw)


def full_run(inp: Path, out: Path, **kw):
    with pipeline(inp, out, **kw) as p:
        preview = p.run_analysis()
    with pipeline(inp, out, **kw) as p:
        done = p.run_copy()
    return preview, done


def output_files(out: Path) -> dict[str, str]:
    """Output-relative path -> content hash, for the copies (not ``.imagory``)."""
    result = {}
    for p in out.rglob("*"):
        rel = p.relative_to(out).as_posix()
        if p.is_file() and not rel.startswith(".imagory/"):
            result[rel] = hash_bytes(p.read_bytes())
    return result


def expected_local(date: dict | None) -> str | None:
    """The ground-truth local date as the pipeline computes it (JSON in the computer's zone);
    None for no date, or a filename date the PRD §8.4 patterns don't recognise."""
    if date is None:
        return None
    if date["taken_at_local"] is not None:
        return date["taken_at_local"]
    utc = dates.datetime.fromisoformat(date["taken_at_utc"].rstrip("Z")).replace(tzinfo=dates.UTC)
    return dates.timestamp_to_local(utc.timestamp())


def group_best(truth: dict, gid: str) -> str | None:
    """The best date (PRD §8.4 across copies, mtime excluded) of a hash group's media."""
    candidates = []
    for rel in truth["groups"][gid]["members"]:
        e = truth["files"][rel[len(truth["source_root"]) + 1 :]]
        date = e["expected_date"]
        if e["kind"] not in ("photo", "other") or date is None:
            continue
        candidates.append(
            dates.DateResult(expected_local(date), date["taken_at_utc"], date["source"])
        )
    best = dates.best_photo_date(candidates)
    return best.taken_at_local if best else None


def media_groups(truth: dict) -> dict[str, list[str]]:
    """Hash group -> its non-Trash photo and other files (input-relative)."""
    groups: dict[str, list[str]] = {}
    for rel, e in truth["files"].items():
        if e["kind"] in ("photo", "other"):
            groups.setdefault(e["hash_group"], []).append(rel)
    return groups


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One complete cleanup of the fake Takeout (stage 3 hashes in the process pool)."""
    with pytest.MonkeyPatch.context() as mp:
        base = tmp_path_factory.mktemp("cleanup")
        mp.setenv("IMAGORY_LIBRARY", str(base / "library"))
        inp, truth = make_input(base / "fake")
        before = snapshot(inp)
        out = base / "output"
        preview, done = full_run(inp, out, pool_threshold=0)
        yield {
            "input": inp,
            "output": out,
            "truth": truth,
            "before": before,
            "preview": preview,
            "done": done,
            "files": output_files(out),
            "by_hash": {h: rel for rel, h in output_files(out).items()},
            "metadata": json.loads(paths.metadata_json_path(out).read_text(encoding="utf-8")),
        }


@pytest.fixture
def out_conn(run):
    conn = db.connect(paths.cleanup_db_path(run["output"]))
    yield conn
    conn.close()


# --- the complete run --------------------------------------------------------------------------


def test_input_is_unchanged(run):
    assert_unchanged(run["input"], run["before"])


def test_done_and_safe_to_delete(run):
    done = run["done"]
    assert done.failed == ()
    assert done.unaccounted == ()
    assert done.safe_to_delete
    assert done.output_path == run["output"]
    assert done.report_path.is_file()
    assert done.copied == len(run["files"]) + sum(
        1 for p in run["output"].joinpath(".imagory", "takeout-json").rglob("*") if p.is_file()
    )


def test_one_output_file_per_hash_group(run):
    truth, files = run["truth"], run["files"]
    groups = media_groups(truth)
    hashes = {truth["groups"][g]["hash"] for g in groups}
    assert sorted(files.values()) == sorted(hashes)  # each exactly once, nothing else


def test_trash_is_skipped_and_reported(run):
    truth = run["truth"]
    trash = sorted(r for r, e in truth["files"].items() if e["kind"] == "trash")
    assert run["preview"].trash_skipped == len(trash)
    trash_only = {
        truth["groups"][e["hash_group"]]["hash"]
        for e in truth["files"].values()
        if e["kind"] == "trash"
    } - {truth["groups"][g]["hash"] for g in media_groups(truth)}
    assert trash_only and not trash_only & set(run["files"].values())
    report = run["done"].report_path.read_text(encoding="utf-8")
    for rel in trash:
        assert rel in report


def test_folders_follow_the_best_date(run):
    truth, by_hash = run["truth"], run["by_hash"]
    for gid, members in media_groups(truth).items():
        if any(truth["files"][m]["companion_of"] for m in members):
            continue  # checked in the motion pair test
        rel = by_hash[truth["groups"][gid]["hash"]]
        assert rel.rsplit("/", 1)[0] == planning.target_folder(group_best(truth, gid)), rel


def test_undated_files_have_no_date_anywhere(run, out_conn):
    truth, by_hash = run["truth"], run["by_hash"]
    meta = {f["path"]: f for f in run["metadata"]["files"]}
    undated = [
        by_hash[truth["groups"][g]["hash"]]
        for g, members in media_groups(truth).items()
        if group_best(truth, g) is None
        and not any(truth["files"][m]["companion_of"] for m in members)
    ]
    assert f"{planning.UNKNOWN_DATE_FOLDER}/clip_nodate.mp4" in undated
    cleaned = next(s for s in db.list_sources(out_conn) if s.type == "cleaned")
    rows = {r.relative_path: r for r in db.list_cleanup_files(out_conn, cleaned.id)}
    extras = {e.relative_path: e for e in db.list_extra_files(out_conn, cleaned.id)}
    for rel in undated:
        assert rel.startswith(planning.UNKNOWN_DATE_FOLDER + "/")
        entry = meta[rel]
        assert (entry["taken_at_local"], entry["taken_at_utc"], entry["date_source"]) == (
            None,
            None,
            None,
        )
        row = rows.get(rel) or extras[rel]
        assert (row.taken_at_local, row.taken_at_utc, row.date_source) == (None, None, None)
    for row in [*rows.values(), *extras.values()]:
        assert row.date_source != "mtime"


def test_copies_get_their_taken_time_as_mtime(run):
    truth, by_hash, out = run["truth"], run["by_hash"], run["output"]
    for gid, members in media_groups(truth).items():
        rel = by_hash[truth["groups"][gid]["hash"]]
        if any(m in CASE_CLASHES for m in members):
            continue  # skipped by the scan on Linux, so its EXIF isn't read (see CASE_CLASHES)
        mtime = (out / rel).stat().st_mtime_ns
        best = group_best(truth, gid)
        if best is not None:
            assert mtime == planning.taken_mtime_ns(best), rel
        else:
            sources = {(run["input"] / m).stat().st_mtime_ns for m in members}
            assert mtime in sources, rel


def test_motion_pair_stays_together(run):
    files, by_hash, truth = run["files"], run["by_hash"], run["truth"]
    heic = by_hash[truth["files"][f"{YEAR04}/MOTION_0001.HEIC"]["hash"]]
    mov = by_hash[truth["files"][f"{YEAR04}/MOTION_0001.MOV"]["hash"]]
    assert os.path.splitext(heic)[0] == os.path.splitext(mov)[0]
    assert heic == "2004/07/MOTION_0001.HEIC" and mov == "2004/07/MOTION_0001.MOV"
    # The album's different HEIC with the same name clashes and gets " (1)".
    album_heic = by_hash[truth["files"][f"{ALBUM}/MOTION_0001.HEIC"]["hash"]]
    assert album_heic == "2004/07/MOTION_0001 (1).HEIC"
    assert set(files) >= {heic, mov, album_heic}


def test_expected_names(run):
    """Only the cases in the todo's "Expected names" table are checked by exact name."""
    by_hash, truth = run["by_hash"], run["truth"]

    def out(rel: str) -> str:
        return by_hash[truth["files"][rel]["hash"]]

    assert out(f"{YEAR04}/a_b_c.jpg") == "2004/05/a_b_c.jpg"  # title a/b:c.jpg
    assert out(f"{YEAR04}/Edited_Test-edited.jpg") == "2004/04/Edited_Test-edited.jpg"
    assert out(f"{YEAR04}/Edited_Test.jpg") == "2004/04/Edited_Test.jpg"
    long_out = out(f"{YEAR04}/Scanned slides from the old shoebox in.jpg")
    assert (
        long_out.rsplit("/", 1)[1]
        == truth["files"][f"{YEAR04}/Scanned slides from the old shoebox in.jpg"]["title"]
    )
    assert len(str(run["output"])) + 1 + len(long_out) <= planning.MAX_PATH_CHARS
    # Case-only clash: the year-folder copy keeps the plain name.
    assert out(f"{YEAR04}/Case_Pair.jpg") == "2004/06/Case_Pair.jpg"
    assert out(f"{ALBUM}/case_pair.JPG") == "2004/06/case_pair (1).JPG"


def test_corrupt_jpeg_and_unused_sidecar_are_copied(run):
    truth, by_hash, out = run["truth"], run["by_hash"], run["output"]
    corrupt = f"{YEAR04}/corrupt_cleanup.jpg"
    assert by_hash[truth["files"][corrupt]["hash"]] == "2004/09/corrupt_cleanup.jpg"
    unused = f"{YEAR04}/UNUSED_cleanup.jpg.json"
    copy = out / planning.JSON_FOLDER / unused
    assert copy.read_bytes() == (run["input"] / unused).read_bytes()
    report = run["done"].report_path.read_text(encoding="utf-8")
    unreadable = report.split("Unreadable files\n")[1].split("\n\n")[0]
    assert unreadable.splitlines() == [corrupt]
    assert unused in report.split("Unmatched sidecars\n")[1].split("\n\n")[0]


def test_every_non_sidecar_json_is_copied(run):
    truth, out = run["truth"], run["output"]
    for rel, e in truth["files"].items():
        if e["kind"] == "json" or (e["kind"] == "sidecar" and e["sidecar_of"] is None):
            assert (out / planning.JSON_FOLDER / rel).is_file(), rel
        if e["kind"] == "sidecar" and e["sidecar_of"]:
            assert not (out / planning.JSON_FOLDER / rel).exists(), rel


def test_albums_match_the_ground_truth(run, out_conn):
    truth, by_hash = run["truth"], run["by_hash"]
    expected: dict[str, set[str]] = {}
    for e in truth["files"].values():
        if e["kind"] not in ("photo", "other"):
            continue
        names = list(e["albums"]) + (["Archive"] if e["folder_kind"] == "archive" else [])
        for name in names:
            expected.setdefault(name, set()).add(by_hash[e["hash"]])
    got = {a["name"]: set(a["files"]) for a in run["metadata"]["albums"]}
    assert got == expected
    assert ALBUM_TITLE in got and "Cleanup album" not in got  # album JSON title wins
    db_albums = {a.name for a in db.list_albums(out_conn)}
    assert db_albums == set(expected)


def test_metadata_json(run):
    meta = run["metadata"]
    assert meta["format"] == "imagory-takeout-cleanup" and meta["version"] == 1
    assert meta["generated_at"].endswith("Z")
    assert [r["id"] for r in meta["runs"]] == [1]
    assert {f["path"] for f in meta["files"]} == set(run["files"])
    by_path = {f["path"]: f for f in meta["files"]}
    p1 = by_path["2004/03/IMG_20040315_101010.jpg"]
    assert p1["kind"] == "photo" and p1["date_source"] == "takeout_json"
    origin_paths = {o["path"] for o in p1["origins"]}
    assert origin_paths == {
        f"{YEAR04}/IMG_20040315_101010.jpg",
        f"{YEAR04}/IMG_20040315_101010 copy.jpg",
        f"{ALBUM}/IMG_20040315_101010.jpg",
    }  # the Trash copy is not an origin
    video = by_path["2004/04/VID_20040401_120000.mp4"]
    assert video["kind"] == "other" and "width" not in video
    assert video["date_source"] == "takeout_json"


def test_output_database(run, out_conn):
    sources = db.list_sources(out_conn)
    assert [s.type for s in sources] == ["cleaned"]  # the temporary source is gone
    (cleaned,) = sources
    rows = db.list_cleanup_files(out_conn, cleaned.id)
    extras = [
        e
        for e in db.list_extra_files(out_conn, cleaned.id)
        if not e.relative_path.startswith(planning.JSON_FOLDER)
    ]
    assert {r.relative_path for r in rows} | {e.relative_path for e in extras} == set(run["files"])
    for r in rows:  # the copies' own size and mtime, so a rescan sees nothing changed
        st = (run["output"] / r.relative_path).stat()
        assert (r.size, r.modified_time) == (st.st_size, st.st_mtime_ns)
    (only_run,) = db.list_runs(out_conn)
    assert only_run.status == "done"
    assert only_run.counts["photos"] + only_run.counts["other_files"] == len(run["files"])
    assert only_run.counts["trash_skipped"] == run["preview"].trash_skipped


def test_preview_counts(run):
    preview, truth, done = run["preview"], run["truth"], run["done"]
    assert preview.photos + preview.other_files + preview.json_files == done.copied
    assert preview.photos + preview.other_files == len(run["files"])
    media = [r for r, e in truth["files"].items() if e["kind"] in ("photo", "other")]
    assert preview.duplicates == len(media) - len(media_groups(truth))
    names = {a for e in truth["files"].values() if e["kind"] != "trash" for a in e["albums"]}
    has_archive = any(e["folder_kind"] == "archive" for e in truth["files"].values())
    assert preview.albums == len(names) + has_archive
    assert preview.unmatched_sidecars == sum(
        1 for e in truth["files"].values() if e["kind"] == "sidecar" and e["sidecar_of"] is None
    )
    assert preview.bytes_to_copy >= sum(
        (run["output"] / rel).stat().st_size for rel in run["files"]
    )
    assert preview.free_bytes > 0


def test_a_done_run_is_not_resumed(run):
    """Picking the same input again starts a new run that copies nothing new."""
    out = run["output"]
    before_out = output_files(out)
    with pipeline(run["input"], out) as p:
        preview = p.run_analysis()
        assert p.run_id == 2
    assert preview.photos == preview.other_files == preview.json_files == 0
    with pipeline(run["input"], out) as p:
        done = p.run_copy()
    assert done.copied == 0 and done.safe_to_delete
    assert output_files(out) == before_out
    assert_unchanged(run["input"], run["before"])


# --- cancel and resume -------------------------------------------------------------------------


def _cancel_when(stage_prefix: str, min_done: int = 1):
    event = threading.Event()

    def progress(stage: str, done: int, total: int) -> None:
        if stage.startswith(stage_prefix) and done >= min_done:
            event.set()

    return event, progress


def _no_duplicate_rows(conn: sqlite3.Connection, run_id: int) -> None:
    origins = db.list_origins(conn, run_id)
    assert len(origins) == len({o.original_relative_path for o in origins})
    copies = db.list_copies(conn, run_id)
    assert len(copies) == len({c.target_relative_path.casefold() for c in copies})
    assert len(copies) == len({c.source_relative_path for c in copies})


@pytest.mark.parametrize("stage", ["Analysing photos", "Analysing other files"])
def test_cancel_during_analysis_then_resume(tmp_path, stage, run):
    inp, _ = make_input(tmp_path / "fake")
    before = snapshot(inp)
    out = tmp_path / "output"
    event, progress = _cancel_when(stage)
    with pipeline(inp, out, progress=progress, cancel_event=event) as p:
        assert p.run_analysis() is None
        assert db.get_run(p.conn, p.run_id).status == "cancelled"
        assert not p.has_plan()
    assert output_files(out) == {}
    with pipeline(inp, out) as p:
        preview = p.run_analysis()
        assert p.run_id == 1
        _no_duplicate_rows(p.conn, p.run_id)
    ref = run["preview"]
    # photos and other files are compared together: see CASE_CLASHES.
    assert (preview.photos + preview.other_files, preview.json_files, preview.duplicates) == (
        ref.photos + ref.other_files,
        ref.json_files,
        ref.duplicates,
    )
    with pipeline(inp, out) as p:
        done = p.run_copy()
    assert done.safe_to_delete
    assert sorted(output_files(out)) == sorted(run["files"])
    assert_unchanged(inp, before)


def test_cancel_during_copy_then_resume(tmp_path):
    inp, _ = make_input(tmp_path / "fake")
    before = snapshot(inp)
    out = tmp_path / "output"
    with pipeline(inp, out) as p:
        assert p.run_analysis() is not None
    event, progress = _cancel_when("Copying", 3)
    with pipeline(inp, out, progress=progress, cancel_event=event, copy_threads=1) as p:
        assert p.run_copy() is None
        run_id = p.run_id
        counts = db.copy_status_counts(p.conn, run_id)
        assert counts["done"] >= 3 and counts["pending"] > 0 and counts["error"] == 0
        done_jobs = db.copy_jobs(p.conn, run_id, "done")
        pending = db.copy_jobs(p.conn, run_id, "pending")
        assert db.get_run(p.conn, run_id).status == "cancelled"
    stamps = {}
    for job in done_jobs:
        st = (out / job.target_relative_path).stat()
        stamps[job.target_relative_path] = (st.st_ino, st.st_ctime_ns, st.st_mtime_ns)
    # A leftover .part from the interrupted copy of a pending target.
    target = out / pending[0].target_relative_path
    part = target.with_name(target.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"partial")
    with pipeline(inp, out) as p:
        done = p.run_copy()
        _no_duplicate_rows(p.conn, run_id)
    assert done.safe_to_delete and done.failed == ()
    assert not part.exists()
    for rel, stamp in stamps.items():  # nothing copied twice
        st = (out / rel).stat()
        assert (st.st_ino, st.st_ctime_ns, st.st_mtime_ns) == stamp, rel
    assert not list(out.rglob("*.part"))
    assert_unchanged(inp, before)


def test_a_changed_copy_is_copied_again(tmp_path):
    inp, _ = make_input(tmp_path / "fake")
    before = snapshot(inp)
    out = tmp_path / "output"
    event, progress = _cancel_when("Copying", 2)
    with pipeline(inp, out) as p:
        p.run_analysis()
    with pipeline(inp, out, progress=progress, cancel_event=event, copy_threads=1) as p:
        assert p.run_copy() is None
        job = next(j for j in db.copy_jobs(p.conn, p.run_id, "done"))
    target = out / job.target_relative_path
    target.unlink()  # gone from the output (not the input)
    with pipeline(inp, out) as p:
        done = p.run_copy()
    assert done.safe_to_delete
    assert hash_bytes(target.read_bytes()) == job.content_hash
    assert_unchanged(inp, before)


# --- failures ----------------------------------------------------------------------------------


def test_an_uncopied_file_blocks_safe_to_delete(tmp_path):
    inp, truth = make_input(tmp_path / "fake")
    before = snapshot(inp)
    out = tmp_path / "output"
    with pipeline(inp, out) as p:
        p.run_analysis()
        job = next(
            j
            for j in db.copy_jobs(p.conn, p.run_id)
            if j.source_relative_path == f"{YEAR04}/IMG_20040315_101010.jpg"
        )
    blocker = out / job.target_relative_path
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_bytes(b"something else")
    with pipeline(inp, out) as p:
        done = p.run_copy()
    assert not done.safe_to_delete
    assert len(done.failed) == 1 and done.failed[0].startswith(job.source_relative_path)
    assert "different content" in done.failed[0]
    assert blocker.read_bytes() == b"something else"  # never overwritten
    group = truth["groups"][truth["files"][job.source_relative_path]["hash_group"]]
    copies = {m[len("Takeout/") :] for m in group["members"] if "/Trash/" not in m}
    sidecar = truth["files"][job.source_relative_path]["sidecar"]
    assert set(done.unaccounted) == copies | {sidecar, f"{ALBUM}/IMG_20040315_101010.jpg.json"}
    report = done.report_path.read_text(encoding="utf-8")
    assert job.source_relative_path in report.split("Not accounted for\n")[1]
    # The duplicates count matches the report's list (the failed photo's copies are gone).
    pairs = [
        line
        for line in report.split("Duplicates\n")[1].split("\n\n")[0].splitlines()
        if line != "(none)"
    ]
    conn = db.connect(paths.cleanup_db_path(out))
    try:
        assert db.list_runs(conn)[0].counts["duplicates"] == len(pairs)
    finally:
        conn.close()
    assert f"duplicates: {len(pairs)}" in report
    assert not any("IMG_20040315_101010" in line for line in pairs)
    assert_unchanged(inp, before)


def test_not_enough_space_refuses_to_copy(tmp_path, monkeypatch):
    inp, _ = make_input(tmp_path / "fake")
    before = snapshot(inp)
    out = tmp_path / "output"
    with pipeline(inp, out) as p:
        p.run_analysis()
    monkeypatch.setattr(CleanupPipeline, "free_bytes", lambda self: 10)
    with pipeline(inp, out) as p, pytest.raises(NotEnoughSpace):
        p.run_copy()
    assert output_files(out) == {}
    assert_unchanged(inp, before)


def test_report_writer_encoding_error_is_caught(tmp_path):
    """A lone surrogate (an undecodable file name) can't be written as UTF-8; the report is
    still written, with the name escaped."""
    p = CleanupPipeline(tmp_path / "in", tmp_path / "out")
    report = CleanupReport({"photos": 1}, ("bad\udcffname.jpg",), (), (), (), (), ())
    with pytest.raises(UnicodeEncodeError):
        export.render_report(report).encode("utf-8")
    path = p._write_report(report)
    assert "bad\\udcffname.jpg" in path.read_text(encoding="utf-8")
    assert not list(path.parent.glob("*.tmp"))
    assert Rename  # the contract type used by reports


def test_output_paths_cannot_leave_the_output(tmp_path):
    p = CleanupPipeline(tmp_path / "in", tmp_path / "out")
    for bad in ("../x.jpg", "/etc/x", "a/../../x", "", "a//b", "./a", "a\\..\\b"):
        with pytest.raises(CleanupError):
            p._output_file(bad)
    assert p._output_file("2019/07/x.jpg") == tmp_path / "out" / "2019" / "07" / "x.jpg"


# --- second account ----------------------------------------------------------------------------


def test_second_run_merges_into_the_output(tmp_path):
    inp1, truth1 = make_input(tmp_path / "first")
    second = _generate(
        tmp_path / "second", "--second-account", str(tmp_path / "first"), "--seed", "2"
    )
    inp2, truth2 = tmp_path / "second" / "Takeout", _truth(second["cleanup"])
    before1, before2 = snapshot(inp1), snapshot(inp2)
    out = tmp_path / "output"
    _, done1 = full_run(inp1, out)
    first_files = output_files(out)
    preview2, done2 = full_run(inp2, out)
    assert done1.safe_to_delete and done2.safe_to_delete
    files = output_files(out)
    # Everything from run 1 is still there, untouched; every hash appears once.
    assert {k: v for k, v in files.items() if k in first_files} == first_files
    assert len(set(files.values())) == len(files)
    all_hashes = {truth1["groups"][g]["hash"] for g in media_groups(truth1)} | {
        truth2["groups"][g]["hash"] for g in media_groups(truth2)
    }
    assert set(files.values()) == all_hashes
    shared = set(second["second_account"]["shared_hashes"])
    new_media = {truth2["groups"][g]["hash"] for g in media_groups(truth2)} - shared
    assert preview2.photos + preview2.other_files == len(new_media)
    # Same names with different content in the same folder get a clash suffix.
    for folder in {k.rsplit("/", 1)[0] for k in files}:
        names = [k.rsplit("/", 1)[1].casefold() for k in files if k.rsplit("/", 1)[0] == folder]
        assert len(names) == len(set(names))
    meta = json.loads(paths.metadata_json_path(out).read_text(encoding="utf-8"))
    assert [r["id"] for r in meta["runs"]] == [1, 2]
    by_path = {f["path"]: f for f in meta["files"]}
    p1 = by_path["2004/03/IMG_20040315_101010.jpg"]
    assert {o["run"] for o in p1["origins"]} == {1, 2}
    album = next(a for a in meta["albums"] if a["name"] == ALBUM_TITLE)
    second_album = by_hash_of(files, truth2, f"{ALBUM}/SECOND_album.jpg")
    assert {"2004/03/IMG_20040315_101010.jpg", second_album} <= set(album["files"])
    conn = db.connect(paths.cleanup_db_path(out))
    try:
        assert [a.name for a in db.list_albums(conn)].count(ALBUM_TITLE) == 1
        assert [s.type for s in db.list_sources(conn)] == ["cleaned"]
    finally:
        conn.close()
    assert_unchanged(inp1, before1)
    assert_unchanged(inp2, before2)


def by_hash_of(files: dict[str, str], truth: dict, rel: str) -> str:
    h = truth["files"][rel]["hash"]
    return next(k for k, v in files.items() if v == h)


# --- folder checks -----------------------------------------------------------------------------


@pytest.fixture
def main_conn(library):
    library.mkdir(parents=True, exist_ok=True)
    conn = db.open_database(paths.db_path(library))
    yield conn
    conn.close()


def test_check_folders_refusals(tmp_path, main_conn, library):
    inp = tmp_path / "takeout"
    inp.mkdir()

    def check(i, o):
        return check_folders(main_conn, library, i, o)

    assert check(tmp_path / "missing", tmp_path / "o").mode == "refused"
    assert check(inp, inp).mode == "refused"
    assert check(inp, inp / "out").mode == "refused"
    nested = tmp_path / "outer"
    (nested / "takeout").mkdir(parents=True)
    assert check(nested / "takeout", nested).mode == "refused"
    assert check(inp, library / "out").mode == "refused"
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    assert check(inp, a_file).mode == "refused"
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "photo.jpg").write_bytes(b"x")
    result = check(inp, busy)
    assert result.mode == "refused" and "empty" in result.message
    plain = tmp_path / "plain"
    plain.mkdir()
    db.create_source(main_conn, str(plain), "Plain", "plain")
    assert check(inp, plain).mode == "refused"


def test_check_folders_modes(tmp_path, main_conn, library):
    inp, _ = make_input(tmp_path / "fake")
    before = snapshot(inp)
    out = tmp_path / "output"
    assert check_folders(main_conn, library, inp, out).mode == "new"
    out.mkdir()
    (out / "desktop.ini").write_text("")
    assert check_folders(main_conn, library, inp, out).mode == "new"
    event, progress = _cancel_when("Analysing other files")
    with pipeline(inp, out, progress=progress, cancel_event=event) as p:
        p.run_analysis()
    assert check_folders(main_conn, library, inp, out).mode == "resume"
    other = tmp_path / "other"
    other.mkdir()
    result = check_folders(main_conn, library, other, out)
    assert result.mode == "refused" and "unfinished" in result.message
    with pipeline(other, out) as p, pytest.raises(CleanupError):
        p.open()
    with pipeline(inp, out) as p:
        p.run_analysis()
    with pipeline(inp, out) as p:
        assert p.run_copy().safe_to_delete
    result = check_folders(main_conn, library, other, out)
    copies = len(output_files(out)) - 1  # not desktop.ini
    assert result.mode == "add" and f"{copies} files" in result.message
    db.create_source(main_conn, str(out), "Cleaned", "cleaned")
    result = check_folders(main_conn, library, other, out)
    assert result.mode == "add" and "rescanned" in result.message
    assert not input_is_source(main_conn, inp)
    db.create_source(main_conn, str(inp), "Takeout", "takeout")
    assert input_is_source(main_conn, inp)
    assert_unchanged(inp, before)


def test_check_folders_warns_about_cloud_folders(tmp_path, main_conn, library):
    inp = tmp_path / "takeout"
    inp.mkdir()
    out = tmp_path / "OneDrive" / "clean"
    result = check_folders(main_conn, library, inp, out)
    assert result.mode == "new" and "cloud" in result.message
    assert storage_warning(tmp_path / "Dropbox" / "x") is not None
    assert storage_warning(tmp_path / "OneDrive - Contoso" / "x") is not None
    assert storage_warning(tmp_path / "photos") is None


def test_check_input(tmp_path):
    inp, _ = make_input(tmp_path / "fake")
    ok, message = check_input(inp)
    assert ok and message
    plain = tmp_path / "plain"
    plain.mkdir()
    ok, message = check_input(plain)
    assert not ok and "continue" in message
    assert check_input(tmp_path / "missing")[0] is False


# --- pure helpers ------------------------------------------------------------------------------


def test_album_dir_of():
    assert album_dir_of("Google Photos/Photos from 2019/a.jpg") is None
    assert album_dir_of("Google Photos/Trip/a.jpg") == "Google Photos/Trip"
    assert album_dir_of("Google Photos/Archive/a.jpg") == "Google Photos/Archive"
    assert album_dir_of("Google Photos/a.json") is None
    assert album_dir_of("archive_browser.html") is None
    assert album_dir_of("Takeout/Google Photos/Trip/sub/a.jpg") == "Takeout/Google Photos/Trip"
    assert album_dir_of("Trip/a.jpg") == "Trip"  # no "Google Photos" folder: the top level


def test_match_other_files():
    names = [
        "VID_1.mp4",
        "MOV_2.mov",
        "clip(1).mp4",
        "clip.mp4",
        "Long video name from a phone x.mp4",
    ]
    jsons = [
        "VID_1.mp4.json",
        "MOV_2.mov.supplemental-metadata.json",
        "clip.mp4.json",
        "clip.mp4(1).json",
        "Long video name from a phone x.mp4.supplemental-m.json",
        "taken.mp4.json",
        "metadata.json",
    ]
    result = match_other_files(names, jsons)
    assert result == {
        "VID_1.mp4": "VID_1.mp4.json",
        "MOV_2.mov": "MOV_2.mov.supplemental-metadata.json",
        "clip.mp4": "clip.mp4.json",
        "clip(1).mp4": "clip.mp4(1).json",
        "Long video name from a phone x.mp4": (
            "Long video name from a phone x.mp4.supplemental-m.json"
        ),
    }
    # A sidecar already used by an image is not offered; an edited copy borrows.
    assert match_other_files(["VID_1.mp4"], jsons, taken={"VID_1.mp4.json"}) == {"VID_1.mp4": None}
    assert match_other_files(["VID_1-edited.mp4", "VID_1.mp4"], jsons) == {
        "VID_1-edited.mp4": "VID_1.mp4.json",
        "VID_1.mp4": "VID_1.mp4.json",
    }
    # A truncated name whose title names another file is rejected.
    long_name = "Holiday video from the mountains with the family.mp4"
    cut = "Holiday video from the mountains with.json"
    assert match_other_files([long_name], [cut]) == {long_name: cut}
    titles = {cut: "Something else entirely.mp4"}
    assert match_other_files([long_name], [cut], read_title=titles.get) == {long_name: None}
    titles = {cut: long_name}
    assert match_other_files([long_name], [cut], read_title=titles.get) == {long_name: cut}


def test_input_with_no_takeout_layout_still_cleans(tmp_path):
    """A plain folder (the wizard warned) is cleaned with the same rules; mtimes never date."""
    from PIL import Image

    inp = tmp_path / "plain"
    (inp / "sub").mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(inp / "IMG_20190131_235959.jpg")
    Image.new("RGB", (8, 8), "blue").save(inp / "sub" / "nodate.png")
    shutil.copy2(inp / "IMG_20190131_235959.jpg", inp / "sub" / "copy.jpg")
    (inp / "notes.txt").write_text("hello")
    before = snapshot(inp)
    out = tmp_path / "output"
    preview, done = full_run(inp, out)
    assert done.safe_to_delete
    assert set(output_files(out)) == {
        "2019/01/IMG_20190131_235959.jpg",
        "Unknown date/nodate.png",
        "Unknown date/notes.txt",
    }
    assert preview.duplicates == 1 and preview.albums == 1  # "sub" is a folder album
    assert_unchanged(inp, before)


def test_a_later_run_dates_a_file_already_in_the_output(tmp_path):
    """Run 1 has an undated photo and video; run 2 brings the same bytes with sidecars. The
    kept rows take the sidecar date (and people), but the files stay in ``Unknown date/``."""
    from PIL import Image

    first = tmp_path / "first"
    year = tmp_path / "second" / "Google Photos" / "Photos from 2019"
    first.mkdir()
    year.mkdir(parents=True)
    Image.new("RGB", (8, 8), "green").save(first / "nodate.png")
    (first / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 500)
    for name in ("nodate.png", "clip.mp4"):
        shutil.copy2(first / name, year / name)
        sidecar = {
            "title": name,
            "photoTakenTime": {"timestamp": "1563000000"},  # 2019-07-13T06:40:00Z
            "people": [{"name": "Ana"}],
        }
        (year / f"{name}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    inp2 = tmp_path / "second"
    before1, before2 = snapshot(first), snapshot(inp2)
    out = tmp_path / "output"
    full_run(first, out)
    _, done = full_run(inp2, out)
    assert done.safe_to_delete and done.copied == 0
    assert set(output_files(out)) == {"Unknown date/nodate.png", "Unknown date/clip.mp4"}
    text = paths.metadata_json_path(out).read_text(encoding="utf-8")
    meta = {f["path"]: f for f in json.loads(text)["files"]}
    for rel in ("Unknown date/nodate.png", "Unknown date/clip.mp4"):
        assert meta[rel]["date_source"] == "takeout_json", rel
        assert meta[rel]["taken_at_utc"] == "2019-07-13T06:40:00Z", rel
        assert meta[rel]["people"] == ["Ana"], rel
    conn = db.connect(paths.cleanup_db_path(out))
    try:
        cleaned = next(s for s in db.list_sources(conn) if s.type == "cleaned")
        (row,) = db.list_cleanup_files(conn, cleaned.id)
        assert row.date_source == "takeout_json"
        (extra,) = db.list_extra_files(conn, cleaned.id)
        assert extra.date_source == "takeout_json" and extra.people == ("Ana",)
    finally:
        conn.close()
    assert_unchanged(first, before1)
    assert_unchanged(inp2, before2)


# --- review fixes ------------------------------------------------------------------------------


def test_run_duplicates_is_linear(tmp_path):
    """20,000 origins, half of them copied: a quick query (it used to be quadratic)."""
    conn = db.open_database(tmp_path / "out.db")
    try:
        run_id = db.create_run(conn, "/in", "in")
        source = db.create_source(conn, "/in", "temp", "takeout")
        n = 20_000
        with db.transaction(conn):
            ids = db.upsert_extra_files(
                conn,
                source.id,
                (db.ExtraFileRecord(f"d/f{i:05d}.bin", i, 0, f"{i:032x}") for i in range(n)),
            )
            db.insert_origins(
                conn,
                run_id,
                (db.OriginRecord(f"d/f{i:05d}.bin", extra_file_id=ids[i]) for i in range(n)),
            )
            db.insert_copies(
                conn,
                run_id,
                (
                    db.CopyRecord(f"d/f{i:05d}.bin", f"t/{i}.bin", extra_file_id=ids[i])
                    for i in range(0, n, 2)
                ),
            )
        started = time.perf_counter()
        result = db.run_duplicates(conn, run_id, source.id)
        elapsed = time.perf_counter() - started
    finally:
        conn.close()
    assert len(result) == n // 2 and result[0] == ("d/f00001.bin", 1)
    assert elapsed < 2, elapsed  # about 0.03 s on WSL


def test_open_refuses_an_existing_photo_folder_and_writes_nothing(tmp_path, library):
    from PIL import Image

    inp = tmp_path / "takeout"
    inp.mkdir()
    photos = tmp_path / "photos"
    photos.mkdir()
    Image.new("RGB", (8, 8), "red").save(photos / "a.jpg")
    before = snapshot(photos)
    for kw in ({}, {"library_root": library}):
        with pytest.raises(CleanupError, match="isn't empty"), pipeline(inp, photos, **kw) as p:
            p.open()
    assert_unchanged(photos, before)
    # The other folder rules, enforced by open() as well as by the wizard.
    plain = tmp_path / "plain"
    source = db.Source(1, "Plain", str(plain), "plain", False, "2026-01-01T00:00:00Z", None)
    cases = [
        (inp, inp / "out", {}),
        (tmp_path / "outer" / "takeout", tmp_path / "outer", {}),
        (inp, library / "out", {}),
        (inp, plain, {"main_sources": [source]}),
    ]
    (tmp_path / "outer" / "takeout").mkdir(parents=True)
    for i, o, kw in cases:
        with pytest.raises(CleanupError), pipeline(i, o, **kw) as p:
            p.open()
        assert not paths.cleanup_dir(o).exists()


def test_a_real_part_file_survives_cancel_and_resume(tmp_path):
    inp = tmp_path / "plain"
    inp.mkdir()
    (inp / "clip.part").write_bytes(b"a real file that happens to end in .part")
    for n in range(1, 6):
        (inp / f"z{n}.txt").write_text(f"file {n}" * 50)
    before = snapshot(inp)
    out = tmp_path / "output"
    with pipeline(inp, out) as p:
        p.run_analysis()
    event, progress = _cancel_when("Copying", 1)
    with pipeline(inp, out, progress=progress, cancel_event=event, copy_threads=1) as p:
        assert p.run_copy() is None
        done = {j.target_relative_path for j in db.copy_jobs(p.conn, p.run_id, "done")}
        pending = {j.target_relative_path for j in db.copy_jobs(p.conn, p.run_id, "pending")}
    assert "Unknown date/clip.part" in done and "Unknown date/z5.txt" in pending
    copied = out / "Unknown date" / "clip.part"
    stamp = copied.stat().st_ino, copied.stat().st_ctime_ns
    stale = out / "Unknown date" / "z5.txt.part"
    stale.write_bytes(b"half")
    with pipeline(inp, out) as p:
        result = p.run_copy()
    assert result.safe_to_delete
    assert copied.read_bytes() == (inp / "clip.part").read_bytes()
    assert (copied.stat().st_ino, copied.stat().st_ctime_ns) == stamp
    assert not stale.exists()
    assert_unchanged(inp, before)


def test_failed_copies_are_retried_on_resume(tmp_path):
    inp, _ = make_input(tmp_path / "fake")
    before = snapshot(inp)
    out = tmp_path / "output"
    with pipeline(inp, out) as p:
        p.run_analysis()
        job = db.copy_jobs(p.conn, p.run_id)[0]
    blocker = out / job.target_relative_path
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_bytes(b"in the way")  # stands in for a drive that dropped out
    with pipeline(inp, out) as p:
        assert p.copy()
        assert db.copy_status_counts(p.conn, p.run_id)["error"] == 1
    blocker.unlink()
    with pipeline(inp, out) as p:
        done = p.run_copy()
    assert done.failed == () and done.safe_to_delete
    assert hash_bytes(blocker.read_bytes()) == job.content_hash
    assert_unchanged(inp, before)


def test_unfinished_run_compares_paths_with_full_casefold():
    def run(i: int, path: str, status: str) -> db.CleanupRun:
        return db.CleanupRun(i, path, "x", "2026-01-01T00:00:00Z", None, status, {})

    runs = [run(1, "/data/Ärger", "cancelled"), run(2, "/data/Ärger", "done")]
    assert unfinished_run(runs, "/data/ärger").id == 1
    assert unfinished_run(runs, "/data/ÄRGER").id == 1
    assert unfinished_run(runs, "/data/Arger") is None


def test_renames_are_stored_at_plan_time(run, out_conn):
    stored = json.loads(db.get_setting(out_conn, "cleanup_renames:1"))
    assert ["2004/05/a/b:c.jpg", "2004/05/a_b_c.jpg", "sanitized"] in stored
    report = run["done"].report_path.read_text(encoding="utf-8")
    assert "2004/05/a/b:c.jpg  ->  2004/05/a_b_c.jpg  (sanitized)" in report


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_an_unreadable_other_file_is_reported(tmp_path):
    inp = tmp_path / "plain"
    inp.mkdir()
    (inp / "ok.txt").write_text("fine")
    locked = inp / "locked.txt"
    locked.write_text("secret")
    before = snapshot(inp)
    locked.chmod(0)  # permissions only: size and mtime stay the same
    try:
        _, done = full_run(inp, tmp_path / "output")
    finally:
        locked.chmod(0o644)
    assert not done.safe_to_delete and "locked.txt" in done.unaccounted
    report = done.report_path.read_text(encoding="utf-8")
    assert "locked.txt" in report.split("Unreadable files\n")[1].split("\n\n")[0]
    assert_unchanged(inp, before)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_check_folders_reads_a_read_only_output_without_writing(tmp_path, main_conn, library):
    inp, _ = make_input(tmp_path / "fake")
    out = tmp_path / "output"
    full_run(inp, out)
    hidden = paths.cleanup_dir(out)
    before = snapshot(hidden)
    hidden.chmod(0o555)
    try:
        result = check_folders(main_conn, library, tmp_path / "fake", out)
    finally:
        hidden.chmod(0o755)
    assert result.mode == "add", result.message
    assert_unchanged(hidden, before)
