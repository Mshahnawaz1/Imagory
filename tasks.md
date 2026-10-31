# Imagory tasks

Scope and design are in `PRD.md`. A task is done only when it is implemented, tested, and
committed. Completed work is listed at the end, one line per task.

## Next: V1 wave 2

Wave 1 is merged (see `docs/v1-agents.md`). Remaining V1 work:

- [ ] §8.3/§8.4 Takeout in the scan: keep JSON names from the walk, `match_directory` per folder, read sidecars, fill `FileJob.takeout_timestamp` and `sidecar_relative_path`, skip Trash, set photo latitude/longitude, edited copies use the original's date, log the matching report
- [ ] §8.1/§8.4 Recompute dates on a source type change (from stored EXIF inputs, filename, mtime, sidecar)
- [ ] §8.7 Filter bar in the main window: switch the grid's row/count queries and scan-row `accepts`; `db.month_counts` for an empty filter; filtered count in the status bar
- [ ] §8.8 Albums in the main window: Albums menu, "Add to album" on toolbar and right-click menu for the selection, album view, "Remove from album"; disable album actions while a scan runs (decided)
- [ ] §8.6 Browsing: viewer zoom/pan, info panel (date and its source, location, source, path, size, dimensions, "Also found in…"), Show in Explorer, "Open in viewer" in the right-click menu
- [ ] Rerun the 100k bench on Windows and judge scrolling by eye

## Open from V0

- [ ] Verify the Windows 10/11 `.exe` and its size (PRD §7.5 item 7). Must be done on real Windows, not WSL

## Completed

### V0
- [x] Project scaffolding: `pyproject.toml`, `uv.lock`, ruff/pytest config, MIT licence, package skeleton
- [x] Library folder resolution and rotating logs (`paths.py`, `logs.py`)
- [x] Database: connection pragmas, migrations with backup, V1 core schema, queries (`db.py`)
- [x] Walk and scan diff (`scanner.py`)
- [x] Metadata, date fallback, and thumbnails (`metadata.py`, `thumbs.py`)
- [x] Scan coordinator: worker pool, batched commits, progress, cancel and resume
- [x] UI: main window, scan thread, thumbnail grid, viewer, reopen from the database
- [x] Fake library generator, benchmark, and 100k results (`docs/v0-results.md`)
- [x] PyInstaller spec and third-party licences (validated on Linux)
- [x] README and PRD kept in sync

### V1
- [x] §8.4 date rules as pure functions: filename dates, priority, time zones, best date across copies (`dates.py`)
- [x] §8.6 virtualized grid prototype with month headers, selection, and a 100k benchmark (`ui/photo_grid.py`)
- [x] Synthetic Takeout export with all sidecar quirks and a ground-truth manifest (`make_fake_library.py --takeout`)
- [x] PRD and README updated for the three items above
- [x] §8.1 Sources: source type and detection, rename, relocate, remove, per-source thumbnails, multi-source scan, Add source and Sources dialogs, grid crash fix (`docs/issues.md` issue 1)
  - deferred: recompute dates on type change (wave 2); album memberships now go with removed photos (cascade)
- [x] Wave 1 (five parallel agents, `docs/v1-agents.md`):
  - §8.5 duplicates: BLAKE2b in the worker, photo identity by hash, primary copy, thumbnails keyed by hash (migration 3); V0 databases are replaced instead of migrated
  - §8.2 moved-file detection, missing-photo rule, "Remove missing…" per source
  - §8.4 dates wired into the scan for plain folders (EXIF with offset, filename, mtime, best date across copies)
  - §8.3 Takeout sidecar matching, reading, Trash rule, matching report (`takeout.py`, not yet used by the scan)
  - §8.6 `PhotoGrid` in the app: month counts, DB data source, thumbnail provider, bench on WSL
  - §8.7 filter queries and `FilterBar` widget (not yet in the main window)
  - §8.8 albums schema (migration 2), queries, Albums and Add-to-album dialogs (not yet in the main window)
