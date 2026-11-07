"""Benchmark the Takeout cleanup pipeline on a generated fake Takeout (PRD §10).

Generates a synthetic Takeout export (``scripts/make_fake_library.py --takeout --cleanup``)
into ``SCRATCH/takeout`` unless it is already there, then runs one :class:`CleanupPipeline`
stage by stage (open, stage 2 photos, stage 3 other files, plan and preview, stage 6 copy,
stage 7 finalise) on a single thread, timing each stage, and prints a table.

The pipeline's input is ``SCRATCH/takeout/Takeout``. The generator's ground-truth
``manifest.json`` sits next to it, outside the input, so it is never walked or copied.

``SCRATCH/output`` and ``SCRATCH/library`` are deleted and recreated on every run. Deletion
needs a marker file (see :func:`remove_tree`), and nothing outside ``SCRATCH`` is written.
The Takeout input is only read.

Usage:
    uv run python scripts/bench_cleanup.py SCRATCH_DIR [--count 100000] [--workers N]

The pipeline starts worker processes, which re-import this script. Everything that does work
is in :func:`main`, called only under the ``__main__`` guard.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from imagory.cleanup_types import DoneResult, PreviewCounts

log = logging.getLogger("bench_cleanup")
T = TypeVar("T")

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
DUPLICATES = 0.05  # the generator's default share of exact copies
SEED = 1  # the generator's default seed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "scratch", type=Path, help="scratch folder for the Takeout, output and library"
    )
    parser.add_argument(
        "--count", type=int, default=100_000, help="photos to generate (default 100000)"
    )
    parser.add_argument(
        "--workers", type=int, default=None, help="worker processes (default: CPUs - 1)"
    )
    return parser.parse_args(argv)


def _is_inside(path: Path, scratch: Path) -> bool:
    """True when ``path`` is a strict descendant of ``scratch``."""
    resolved, base = path.resolve(), scratch.resolve()
    return resolved != base and base in resolved.parents


def remove_tree(path: Path, scratch: Path, marker: str | None) -> None:
    """Delete a folder this benchmark made. It must lie inside ``scratch`` and contain
    ``marker`` (a path relative to ``path``) that only our own runs create. An empty folder
    is removed with rmdir. Anything else stops the run, so nothing is deleted by guesswork."""
    if not path.exists():
        return
    if not _is_inside(path, scratch):
        raise SystemExit(f"Refusing to delete {path}: it is not inside {scratch}")
    if marker is not None and (path / marker).exists():
        shutil.rmtree(path)
    elif path.is_dir() and not any(path.iterdir()):
        path.rmdir()
    else:
        raise SystemExit(
            f"Refusing to delete {path}: no {marker or 'marker'} inside it. Delete it by hand."
        )
    log.info("Removed %s", path)


def _read_manifest(path: Path) -> tuple[int, int]:
    """Return ``(count, images)`` from the generator's manifest, without keeping the JSON."""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    result = (int(manifest["count"]), len(manifest["images"]))
    del manifest
    return result


def fmt_row(cells: tuple[str, ...], widths: list[int]) -> str:
    """Left-align the first and last cells (stage, note) and right-align the numbers."""
    last = len(cells) - 1
    return "  ".join(
        cell.ljust(width) if i in (0, last) else cell.rjust(width)
        for i, (cell, width) in enumerate(zip(cells, widths, strict=True))
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    scratch: Path = args.scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    library = scratch / "library"
    takeout_root = scratch / "takeout"
    takeout_dir = takeout_root / "Takeout"
    output = scratch / "output"

    # Set before any import from imagory: paths.library_dir() reads it when it is called.
    os.environ["IMAGORY_LIBRARY"] = str(library)
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(SCRIPTS_DIR))

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    from make_fake_library import make_takeout

    from imagory.cleanup_run import CleanupPipeline

    # Generate the fake Takeout once. The generator writes manifest.json last, so a folder
    # without it is an interrupted generation and is removed before generating again.
    manifest_path = takeout_root / "manifest.json"
    generate_seconds = 0.0
    if manifest_path.exists():
        manifest_count, manifest_images = _read_manifest(manifest_path)
        if manifest_count != args.count:
            raise SystemExit(
                f"The Takeout in {takeout_root} was made with --count {manifest_count}, not "
                f"{args.count}. Delete {takeout_root} or pass --count {manifest_count}."
            )
        log.info("Reusing the fake Takeout in %s", takeout_root)
    else:
        remove_tree(takeout_root, scratch, marker="Takeout")
        takeout_root.mkdir(parents=True, exist_ok=True)
        log.info("Generating a fake Takeout of %s photos in %s", f"{args.count:,}", takeout_root)
        started = time.perf_counter()
        make_takeout(takeout_root, args.count, DUPLICATES, SEED, cleanup=True)
        generate_seconds = time.perf_counter() - started
        log.info("Generated the fake Takeout in %.1f s", generate_seconds)
        manifest_count, manifest_images = _read_manifest(manifest_path)

    # A fresh output and library on every run. Each must hold its marker, so a folder we did
    # not make is never deleted.
    remove_tree(output, scratch, marker=".imagory/imagory.db")
    remove_tree(library, scratch, marker="imagory.db")

    last_stage = ""
    last_log = 0.0

    def progress(stage: str, done: int, total: int) -> None:
        nonlocal last_stage, last_log
        now = time.perf_counter()
        if stage != last_stage or now - last_log >= 10:
            last_stage, last_log = stage, now
            log.info("%s %s/%s", stage, f"{done:,}", f"{total:,}" if total else "?")

    seconds: dict[str, float] = {}

    def timed(name: str, fn: Callable[[], T]) -> T:
        started = time.perf_counter()
        result = fn()
        seconds[name] = time.perf_counter() - started
        log.info("%s finished in %.1f s", name, seconds[name])
        return result

    def must(ok: bool, name: str) -> None:
        if not ok:
            raise SystemExit(f"{name} was cancelled")

    def plan_and_preview() -> PreviewCounts:
        if not pipe.plan():
            raise SystemExit("plan was cancelled")
        return pipe.preview()

    with CleanupPipeline(
        takeout_dir,
        output,
        library_root=library,
        workers=args.workers,
        progress=progress,
    ) as pipe:
        log.info("Using %d worker processes", pipe.workers)
        timed("open", pipe.open)
        must(timed("2 analyse_photos", pipe.analyse_photos), "2")
        must(timed("3 analyse_other_files", pipe.analyse_other_files), "3")
        # Read the counts after stage 3, outside every timer: stage 3 has built the inventory
        # and cached it. Reading after stage 2 would walk the input again (stage 2 clears the
        # cache) and that walk would be missing from stage 3's time.
        inventory = pipe.inventory()
        preview = timed("4-5 plan + preview", plan_and_preview)
        must(timed("6 copy", pipe.copy), "6")
        done: DoneResult = timed("7 finalise", pipe.finalise)
        workers = pipe.workers

    items_for: dict[str, tuple[int, str]] = {
        "2 analyse_photos": (len(inventory.images), "photos"),
        "3 analyse_other_files": (len(inventory.files), "files in input (non-Trash)"),
        "6 copy": (done.copied, "copies"),
    }
    table: list[tuple[str, str, str, str, str]] = []
    for name, secs in seconds.items():
        count, basis = items_for.get(name, (None, ""))
        rate = f"{count / secs:,.1f}" if count and secs > 0 else "-"
        table.append(
            (
                name,
                f"{secs:,.1f}",
                f"{count:,}" if count is not None else "-",
                rate,
                basis or "-",
            )
        )
    total = ("Pipeline total", f"{sum(seconds.values()):,.1f}", "-", "-", "open to finalise")
    header = ("Stage", "Seconds", "Items", "Items/s", "Items are")
    all_rows = [header, *table, total]
    widths = [max(len(row[i]) for row in all_rows) for i in range(5)]

    print()
    print(f"Platform:   {platform.platform()}")
    print(f"Python:     {platform.python_version()}  CPUs: {os.cpu_count()}")
    print(f"Count:      {args.count:,} requested, {manifest_images:,} images in the manifest")
    print(f"Workers:    {workers}")
    print(
        f"Input:      {len(inventory.files):,} files outside Trash, "
        f"{len(inventory.trash):,} in Trash, in {takeout_dir}"
    )
    print()
    print(fmt_row(header, widths))
    for row in table:
        print(fmt_row(row, widths))
    print(fmt_row(total, widths))
    if generate_seconds:
        print(f"Generation (not in the pipeline): {generate_seconds:,.1f} s")
    else:
        print("Generation (not in the pipeline): reused an existing Takeout")
    print()
    print(
        f"Preview:    {preview.photos:,} photos, {preview.other_files:,} other files, "
        f"{preview.duplicates:,} duplicates, {preview.trash_skipped:,} trash skipped"
    )
    failed, unaccounted = len(done.failed), len(done.unaccounted)
    print(f"Copied:     {done.copied:,} (failed {failed:,}, unaccounted {unaccounted:,})")
    print(f"Safe to delete check: {done.safe_to_delete}")
    print(f"Output:     {output}")


if __name__ == "__main__":
    main()
