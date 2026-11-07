"""Benchmark the Takeout cleanup pipeline on a generated fake Takeout (PRD §10).

Generates a synthetic Takeout export (``scripts/make_fake_library.py --takeout --cleanup``)
into ``SCRATCH/takeout`` unless it is already there, then runs one :class:`CleanupPipeline`
stage by stage (open, stage 2 photos, stage 3 other files, plan and preview, stage 6 copy,
stage 7 finalise) on a single thread, timing each stage, and prints a table.

``SCRATCH/output`` and ``SCRATCH/library`` are deleted and recreated on every run. Nothing
outside ``SCRATCH`` is written, and the Takeout input is only read.

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
from pathlib import Path

log = logging.getLogger("bench_cleanup")

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


def remove_tree(path: Path, scratch: Path) -> None:
    """Delete a folder this benchmark owns, refusing anything outside the scratch folder."""
    if not path.exists():
        return
    if not _is_inside(path, scratch):
        raise SystemExit(f"Refusing to delete {path}: it is not inside {scratch}")
    shutil.rmtree(path)
    log.info("Removed previous %s", path)


def count_files(folder: Path) -> int:
    return sum(len(files) for _, _, files in os.walk(folder))


def fmt_seconds(seconds: float) -> str:
    return f"{seconds:,.1f}"


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    scratch: Path = args.scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    library = scratch / "library"
    takeout_dir = scratch / "takeout"
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

    # Generate the fake Takeout once; the generator writes manifest.json last.
    manifest_path = takeout_dir / "manifest.json"
    generate_seconds = 0.0
    if manifest_path.exists():
        log.info("Reusing the fake Takeout in %s", takeout_dir)
    else:
        takeout_dir.mkdir(parents=True, exist_ok=True)
        log.info("Generating a fake Takeout of %s photos in %s", f"{args.count:,}", takeout_dir)
        started = time.perf_counter()
        make_takeout(takeout_dir, args.count, DUPLICATES, SEED, cleanup=True)
        generate_seconds = time.perf_counter() - started
        log.info("Generated the fake Takeout in %.1f s", generate_seconds)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("count") != args.count:
        log.warning(
            "The Takeout in %s was made with count %s, not %s; delete it to regenerate",
            takeout_dir,
            manifest.get("count"),
            args.count,
        )
    photos_in_manifest = len(manifest["images"])
    input_files = count_files(takeout_dir)

    # A fresh output and library on every run.
    remove_tree(output, scratch)
    remove_tree(library, scratch)

    state: dict[str, object] = {"stage": None, "at": 0.0}

    def progress(stage: str, done: int, total: int) -> None:
        now = time.perf_counter()
        if stage != state["stage"] or now - float(state["at"]) >= 10:  # type: ignore[arg-type]
            state["stage"], state["at"] = stage, now
            shown = f"{total:,}" if total else "?"
            log.info("%s %s/%s", stage, f"{done:,}", shown)

    rows: list[tuple[str, float, int | None, str]] = []

    def timed(name: str, items: int | None, basis: str, fn):  # noqa: ANN001, ANN202
        started = time.perf_counter()
        result = fn()
        seconds = time.perf_counter() - started
        rows.append((name, seconds, items, basis))
        log.info("%s finished in %.1f s", name, seconds)
        return result

    def must(ok: bool, name: str) -> None:
        if not ok:
            raise SystemExit(f"{name} was cancelled")

    with CleanupPipeline(
        takeout_dir,
        output,
        library_root=library,
        workers=args.workers,
        progress=progress,
    ) as pipe:
        log.info("Using %d worker processes", pipe.workers)
        timed("open", None, "", pipe.open)
        must(timed("2 analyse_photos", photos_in_manifest, "photos", pipe.analyse_photos), "2")
        must(
            timed("3 analyse_other_files", input_files, "files in input", pipe.analyse_other_files),
            "3",
        )

        def plan_and_preview():  # noqa: ANN202
            if not pipe.plan():
                raise SystemExit("plan was cancelled")
            return pipe.preview()

        preview = timed("4-5 plan + preview", None, "", plan_and_preview)
        must(timed("6 copy", None, "", pipe.copy), "6")
        done = timed("7 finalise", None, "", pipe.finalise)

    # Items per second for stage 6 come from the copies it made, not the input size.
    table_rows = []
    for name, seconds, items, basis in rows:
        if name == "6 copy":
            items, basis = done.copied, "copies"
        rate = f"{items / seconds:,.1f}" if items and seconds > 0 else "-"
        table_rows.append((name, fmt_seconds(seconds), f"{items:,}" if items else "-", rate, basis))
    pipeline_total = sum(seconds for _, seconds, _, _ in rows)

    print()
    print(f"Platform:   {platform.platform()}")
    print(f"Python:     {platform.python_version()}  CPUs: {os.cpu_count()}")
    print(f"Count:      {args.count:,} requested, {photos_in_manifest:,} images in the manifest")
    print(f"Workers:    {pipe.workers}")
    print(f"Input:      {input_files:,} files in {takeout_dir}")
    print()
    header = ("Stage", "Seconds", "Items", "Items/s", "Items are")
    widths = [max(len(header[i]), *(len(r[i]) for r in table_rows)) for i in range(5)]
    widths[0] = max(widths[0], len("Pipeline total"))
    print(
        "  ".join(
            h.ljust(w) if i in (0, 4) else h.rjust(w)
            for i, (h, w) in enumerate(zip(header, widths, strict=True))
        )
    )
    for row in table_rows:
        print(
            "  ".join(
                c.ljust(w) if i in (0, 4) else c.rjust(w)
                for i, (c, w) in enumerate(zip(row, widths, strict=True))
            )
        )
    total_row = ("Pipeline total", fmt_seconds(pipeline_total), "-", "-", "open to finalise")
    print(
        "  ".join(
            c.ljust(w) if i in (0, 4) else c.rjust(w)
            for i, (c, w) in enumerate(zip(total_row, widths, strict=True))
        )
    )
    if generate_seconds:
        print(f"Generation (not in the pipeline): {fmt_seconds(generate_seconds)} s")
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
