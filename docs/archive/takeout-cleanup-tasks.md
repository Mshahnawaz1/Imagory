# Takeout cleanup: original plan and tasks (archived)

Finished on 2026-11-07. The live design is `docs/takeout-cleanup.md`. Kept for history; module and migration names below are as they were then.

Make a clean copy of an extracted Google Takeout folder. The copy has one file per unique photo,
no JSON sidecars, and a Year/Month layout, plus a database and a `metadata.json` that keep all the
Takeout metadata. Afterwards the user can delete the original Takeout and lose nothing.

Status: **planned, not started.** The main decisions came from a Q&A with the author on 2026-11-07:
one copy per photo, copying everything else as-is, the Year/Month layout, adding the output as a
`cleaned` source that reads its own database, albums and Archive as albums, keeping both edited and
original copies, one input per run with later runs merged in, skipping Trash, the swap offer, the
title-based filenames, and `metadata.json`. Everything else is a proposed default, open to change.
This file tracks only this feature. The project-wide list is `docs/tasks.md`. Tick a box only
when the item is implemented, tested, and committed.

## PRD impact (update `PRD.md` first, task 0)

This feature changes agreed scope, so the PRD must change in the same work:

- **§3 non-goals** list "Deleting or moving files on disk, including duplicate removal". Cleanup
  never deletes or moves anything, but it does **write copies** to a folder the user picks. §14 lists
  "optional export of organized copies" under *Later*; this feature brings it forward.
- **Principle 1** ("never writes app data into photo folders") gets one exception: a folder that
  Imagory created as a cleanup output may hold `.imagory/` (database, thumbnails, `metadata.json`,
  reports). Originals stay strictly read-only.
- **§3 non-goal "Importing Takeout albums as Imagory albums"** is lifted for cleanup: Takeout album
  folders (and Archive) become albums.
- **§8.4 dates:** cleanup and `cleaned` sources never use the file modified time; a file with no sidecar, EXIF or filename date stays undated.
- **§9 schema:** a new source type `cleaned` and new tables (migration 5).
- **§16 decisions log:** one row per decision in the table below.
- Zips stay V1.5. The input must be an extracted folder.

## Tasks

### 0. Docs first
- [x] `PRD.md`: new section (§8.10 Takeout cleanup), plus edits to §3, §9, §14 and §16 as listed under PRD impact
- [x] `docs/tasks.md`: link to this file under upcoming work

### 1. Pure planning (`cleanup.py`, no Qt, no SQL)
- [x] Kept-copy choice (existing output files win), target folder from the date (sidecar, EXIF, filename, never mtime; otherwise `Unknown date`), name from title with extension rules, the `-edited` rule, and companion files following their photo
- [x] Sanitizing for Windows names, casefold clash resolution against planned and existing names, long-path shortening
- [x] Free-space estimate. Unit tests for all of the above, including odd titles (empty, only dots, reserved names, emoji, 300 characters)

### 2. Schema and SQL
- [x] Migration 5 (sources rebuild for `cleaned`, new tables, indexes). Migration test from version 4 with data, backup written, foreign keys intact
- [x] Queries: runs, origins, extra files, copies, finalise transaction, `metadata.json` export rows

### 3. Analyse and plan
- [x] Analyse photos with the existing `Scanner` into the output database (temporary input source)
- [x] Full-inventory walk and worker hashing, sidecar match for videos, `extra_files` rows for non-images, unreadable images and JSON files that aren't sidecars
- [x] Planning writes `file_origins` and `cleanup_copies`. Integration test on the fake Takeout: duplicates collapse, album and Archive membership recorded, Trash skipped, **input unchanged**

### 4. Copy, verify, finalise
- [x] Copy with `.part`, source hash check, re-read verification, rename, mtime. One unreadable file is counted and the run continues
- [x] Cancel mid-copy, then resume: no duplicate rows, `done` files not copied again, leftover `.part` files cleaned up
- [x] Finalise transaction (stores the copies' own size and mtime), albums (album JSON title if present), `metadata.json` (validated against the format above), report
- [x] "Safe to delete" check against the full inventory. Test: a corrupt image and an unused sidecar end up in the output, and an uncopied file blocks the message
- [x] Second run into an existing output (second fake account): deduplicated against existing files, clashes renamed, `metadata.json` covers both runs
- [x] Every integration test asserts the **input folder is unchanged** (same files, sizes and mtimes, no new files)

### 5. Main library import
- [x] `cleaned` source type: the scan reads metadata from the output database by path and hash, and keeps `takeout_json` dates, people, caption, favourite and location. Files stored with no date stay undated (no mtime fallback). Falls back to plain rules for unknown files
- [x] Add to library: source added and scanned, the run's albums created once, swap offer removes the old source only after confirmation. Test that photo ids, dates, people and albums survive the swap
- [x] Output folder moved or renamed: relocate the source and the metadata still resolves
- [x] Sources dialog and Add source: `cleaned` detected and shown, no "Change type…", `recompute_dates` reads the output database. A second cleanup run into an output that is already a source is allowed and rescans it

### 6. UI
- [x] `CleanupThread`, the only writer of the output database, with progress and cancel signals
- [x] Wizard pages 1 to 6, toolbar button, File menu item, actions disabled around scans and cleanups
- [x] UI tests (offscreen): page flow, refusal messages, cancel prompt on close
- [ ] Light and dark theme check by eye (on Windows, part of the handoff)

### 7. Tools and docs
- [x] Fake Takeout generator additions (see Modules) and manifest ground truth for the cleanup
- [x] README: what cleanup does, that originals are never touched or deleted, disk space needed, the offline promise kept prominent
- [x] PRD §10: time a cleanup of the fake Takeout (analyse plus copy) and record it. Done at 50,000 photos on WSL (author's choice); 100,000 and Windows still open
- [x] Final check: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`

## Open questions

1. ~~Album name clash in the main library~~: decided 2026-11-07, a new `<name> (Takeout)` album
   (see Decisions).
2. ~~`Unknown date` for files without a real date~~: decided 2026-11-07, sidecar, then EXIF, then
   filename, never mtime (see Decisions).
3. ~~Video placement~~: decided 2026-11-07, sidecar date, then filename date, otherwise
   `Unknown date`. Container dates (MP4 `creation_time`) are not read, to avoid a new dependency.
