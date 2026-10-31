# Imagory tasks

Scope and design are in `PRD.md`. A task is done only when it is implemented, tested, and
committed. Completed work is listed at the end, one line per task.

## Next: V1

- [ ] §8.2 Missing files: moved-file detection by hash, a photo is missing only when all its files are, "Remove missing items" per source
- [ ] §8.3 Takeout import: sidecar matching with all naming quirks and a matching report; read `photoTakenTime`, `geoData`, `title`; skip `Trash`
- [ ] §8.4 Dates: wire `imagory/dates.py` into the scanner and db
- [ ] §8.5 Duplicates: BLAKE2b hash in the worker pass, photo identity by hash, primary copy, thumbnails keyed by hash, V0 migration
- [ ] §8.6 Grid integration:
  - month counts in db.py
  - a DB data source for the grid
  - thumbnail provider
  - replace `PhotoGridView` with `PhotoGrid`
  - rerun the 100k bench on Windows
- [ ] §8.6 Browsing: multi-select actions, viewer zoom/pan, info panel, Show in Explorer
- [ ] §8.7 Search and filters: text search; date, source, folder, album, has-duplicates, missing
- [ ] §8.8 Albums: tables, create/rename/delete, add/remove photos, album view

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
  - deferred: recompute dates on type change (needs §8.3/§8.4 wiring); remove album memberships (needs §8.8)
