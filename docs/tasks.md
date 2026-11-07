# Imagory tasks

Scope and design are in `PRD.md`. A task is done only when it is implemented, tested, and
committed. Completed work is listed at the end, one line per task.

## Next: V1 wave 2

Waves 1 and 2 are merged. Remaining V1 work:

- [ ] Rerun the 100k bench on Windows and judge scrolling by eye. Judged smooth by eye on Windows 11 (.exe, 100,000 plain photos, full scan about 10 minutes); `scripts/bench.py` numbers not yet taken on Windows
- [ ] Retest "Show in Explorer" on Windows: the first Windows 11 test opened a default folder instead of the photo's (paths with spaces); fixed, untested on Windows

## Takeout cleanup: Windows checks

The feature is merged (see Completed). Still to check on real Windows, not WSL:

- [ ] Paths near 250 characters, and reserved names (`CON`, `NUL`, …) in the output
- [ ] Copies' modified time on NTFS (set to the taken time; 1 µs tolerance on resume)
- [ ] The wizard in light and dark theme, by eye
- [ ] A full cleanup and Add to library from the `.exe`, and closing the app during a copy
- [ ] `scripts/bench_cleanup.py` at 100,000 photos (only 50,000 on WSL so far)
- [ ] A real Takeout export: title quirks and video sidecar matching (PRD §18 question 9)

## Open from V0

- [ ] Verify the Windows 10/11 `.exe` and its size (PRD §7.5 item 7). Must be done on real Windows, not WSL. Windows 11 done (49.4 MB, starts, scans plain and Takeout folders); Windows 10 still to test

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
- [x] Wave 2 (three parallel agents, branches merged into `main`):
  - §8.3/§8.4 Takeout in the scan: sidecars matched per folder, Trash skipped, takeout timestamp and photo location stored, edited copies inherit date and location, matching report logged
  - §8.1/§8.4 `sources.change_source_type` recomputes dates; "Change type…" in the Sources dialog uses it (atomic, warns if a Takeout folder is unavailable)
  - §8.7 filter bar wired into the main window; filtered count in the status bar
  - §8.8 Albums menu, "Add to album" (toolbar and right-click), album view, "Remove from album", disabled during scans
  - §8.6 viewer zoom/pan, info panel, Show in Explorer, "Open in viewer" in the right-click menu
- [x] People from Takeout: people, caption, favourite, upload time read from sidecars; People sidebar section, person filter and search, info panel rows (`docs/people/tasks.md`, PRD §8.9). Theme check by eye still open
- [x] Takeout cleanup (PRD §8.10, `docs/takeout-cleanup-plan.md`, four agent waves): migration 5 and `cleaned` sources, planning, verified copy, `metadata.json` and report, fake Takeout cases, wizard, pipeline, main window wiring, benchmark (50,000 photos on WSL in about 6 minutes)
