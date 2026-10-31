# Imagory: Product Requirements Document

Version 1.2 · October 31, 2026 · Status: V0 implemented (Windows checks pending, section 7.5). V1 in progress: section 8.1 (sources) implemented. Remaining open items are listed in section 18.

## 1. Overview

Imagory is a free, open source, fully offline Windows application that helps non-technical people browse and organize photo collections stored in local folders, including Google Photos exports (Google Takeout). It indexes photos into a local SQLite database, shows them in a fast thumbnail grid and viewer, detects exact duplicates, and lets users search, filter, and group photos into albums. It never modifies, moves, or deletes the user's original files.

**Problem:** People who leave Google Photos end up with Takeout exports where metadata sits in separate JSON files, dates are often wrong, and duplicates are common. Existing open source options (Immich, PhotoPrism) are server-based and need Docker or technical setup. digiKam is a full desktop app but is feature-dense and, as far as research found, does not obviously repair Takeout metadata (unverified).

**Positioning:** A simple Windows app: point it at folders, get a clean browsable library.

## 2. Target users

- **Primary (eventually):** non-technical Windows users with photos in local folders or a Google Takeout export.
- **Right now:** the author, for personal use. Open source from the start.

## 3. Principles and non-goals

**Principles**

1. **Read-only on originals.** The app only reads photo files and stores their paths. It never moves, edits, renames, or deletes them, and never writes app data into photo folders.
2. **Fully offline.** No telemetry, accounts, update checks, or network calls in V0 and V1.
3. **Disposable cache, durable library.** Thumbnails can be deleted and regenerated at any time. The database is the source of truth for Imagory's own data (sources, albums, dates).
4. **Hide, never destroy.** Duplicates are hidden from view, not deleted. The database is never purged automatically; removal of database rows only happens through an explicit, confirmed user action.

**Non-goals (V0 and V1)**

- Deleting or moving files on disk, including duplicate removal.
- Face recognition (planned for V2).
- RAW and video support.
- Cloud sync, mobile apps, multi-user, accounts.
- Photo editing.
- Near-duplicate (visually similar) detection.
- Map view.
- Portable mode (data stored next to the .exe).
- Importing Takeout albums as Imagory albums.

## 4. Platform and technology

| Item | Decision |
| --- | --- |
| Platform | Windows 10 and Windows 11 (64-bit) |
| Language | Python 3.12 |
| Project and dependency management | uv (`pyproject.toml` plus a committed `uv.lock`) |
| UI framework | PySide6 (Qt 6) |
| Storage | SQLite (Python built-in sqlite3) |
| Image reading | Pillow, plus pillow-heif for HEIC |
| Hashing | BLAKE2b from Python hashlib (built in, fast), read in 1 MB chunks |
| Packaging | PyInstaller. Single-file .exe first, zipped folder build added later (section 13) |
| Tests | pytest |
| License | MIT |
| Distribution | GitHub releases, no payment, no code signing |
| Other platforms | Not in scope, but avoid Windows-only code where it costs nothing |

## 5. Roadmap

| Version | Scope | Status |
| --- | --- | --- |
| **V0** | Pick a directory, scan it, preview images, store image data in SQLite | Confirmed |
| **V1** | Multiple sources, Takeout folder import, exact duplicate detection, month-grouped thumbnail grid, photo viewer, search and filters, manual albums | Confirmed |
| **V1.x** | Zipped folder build published alongside the single .exe | Confirmed, timing flexible |
| **V1.5** | Takeout import from .zip files, including multi-part exports | Confirmed as after V1 |
| **V2** | Face detection and recognition as a separate downloadable extension | Proposed, details not confirmed |

## 6. Architecture

### 6.1 Components

The code is split into small modules with one job each. The UI never touches files or the database directly during a scan.

| Module | Responsibility |
| --- | --- |
| `db` | Connection setup (WAL, foreign keys), schema migrations, all SQL queries |
| `scanner` | Walks a source, compares it to the database, queues work, applies results |
| `metadata` | Reads dimensions, format, EXIF date, GPS, orientation; parses dates from filenames |
| `takeout` | Detects Takeout sources, finds and parses JSON sidecars |
| `thumbs` | Generates and locates cached thumbnails |
| `ui` | Main window, grid, viewer, dialogs |

### 6.2 Concurrency

- **Main thread:** Qt UI only.
- **Scan coordinator (one QThread):** walks the folders, sends work to the process pool, and is the **only writer** to the database during a scan. Results are committed in batches (around 200 files per transaction) so work is never lost in large chunks.
- **Worker processes (ProcessPoolExecutor, CPU count minus one):** for each file they read metadata, compute the hash (V1 and later), and generate the thumbnail, all in a single read of the file where possible. Workers return plain results and **never write to the database.**
- **Thumbnail loader (QThreadPool):** loads cached thumbnail files for the visible part of the grid. Decoded images are kept in a size-limited in-memory cache.
- `multiprocessing.freeze_support()` must be called at startup, or worker processes fail inside a PyInstaller build.

### 6.3 Scan pipeline

1. **Walk** the source with `os.scandir`, collecting path, size, and modified time. This step is fast and does not open files.
2. **Diff** against the `files` table: new, changed (size or modified time differs), unchanged, disappeared.
3. **Process** new and changed files in the worker pool (metadata, hash, thumbnail). Unchanged files are skipped entirely.
4. **Apply** results: insert or update file rows, attach each file to a photo by its hash, and recompute the photo's primary copy and date.
5. **Mark** disappeared files as missing (section 8.2).
6. **Record** a scan run with its counts.

**Cancel and resume need no saved state.** Every finished file is committed, so an interrupted scan simply runs again and skips files that are already done. Cancelling stops queueing new work and lets in-flight results commit.

### 6.4 Library folder

- Default: `%LOCALAPPDATA%\Imagory\`, containing `imagory.db`, `thumbs\`, and `logs\`. On other platforms, which are used only for development, it is `$XDG_DATA_HOME/imagory` (default `~/.local/share/imagory`). An `IMAGORY_LIBRARY` environment variable overrides the location, for tests and development.
- V1: the user can choose another library folder in settings. The choice is stored in `%LOCALAPPDATA%\Imagory\config.json`, which is always read first at startup.
- Warn (but allow) if the chosen library folder is inside OneDrive, Dropbox, Google Drive, or a network share, because syncing can corrupt SQLite databases.

### 6.5 Thumbnails

- One size: 320 px on the longest edge, JPEG quality 85.
- Stored at `thumbs\ab\cd\<hash>.jpg` (the first two byte pairs of the hash as subfolders), so that no folder holds 100,000 files. Two exact-duplicate files share one thumbnail.
- **EXIF orientation is applied** (Pillow `ImageOps.exif_transpose`) to both thumbnails and the viewer, otherwise phone photos appear sideways.
- JPEG decoding uses Pillow's draft mode to decode at reduced size, which makes thumbnail generation several times faster.
- GIFs use the first frame.
- **Missing thumbnails are regenerated on demand** by the thumbnail loader, so the cache can be deleted at any time (principle 3).
- V0 has no content hashes, so its thumbnails are keyed by the BLAKE2b hash of the file's relative path (ASCII case-folded, like `COLLATE NOCASE`, so a case-only rename keeps its thumbnail) and use the same folder layout. Workers can then write thumbnails directly without knowing the database id. On the V1 migration, the V0 cache is simply deleted and regenerated.

### 6.6 Logging and errors

- A rotating log file in `logs\` records scan summaries, unreadable files, and exceptions. It never contains anything sent anywhere.
- A Help menu item opens the log folder, so a non-technical user can attach logs to a GitHub issue.
- A single corrupt or unreadable image never stops a scan. It is logged, counted as an error in the scan summary, and skipped.

## 7. V0 requirements

Goal: prove the core loop end to end with minimal features: pick a folder, scan it in the background, browse thumbnails, view a photo, and reopen the library later without rescanning.

### 7.1 Library and source

- **V0 uses the V1 core schema** (sources, files, photos, scan_runs, settings) with exactly one source, created when the user picks a folder. Each file is its own photo, and hash columns are left empty. This avoids a difficult migration later (section 9).
- The schema is versioned from the start using `PRAGMA user_version`, with a migration function for every version step.
- **First launch:** an empty window with a "Choose folder" button. Choosing a folder creates the source and starts a scan.
- **Later launches:** the grid is shown straight from the database. No scan starts automatically.
- **Rescan** (toolbar button): runs the incremental scan from section 6.3. Unchanged files are skipped, changed files are reprocessed, and files that have disappeared are marked missing.
- **Change folder:** after confirmation, deletes the current source's rows (files, photos, and scan_runs) and its thumbnails, then creates the new source and scans it. Files on disk are never touched.
- The library folder is always the default location (section 6.4). There is no settings screen in V0.

### 7.2 Scanning

- Recursive scan. Supported extensions, compared case-insensitively: `.jpg`, `.jpeg`, `.png`, `.gif`, `.heic`, `.heif`. Other files are skipped and counted by extension.
- Walk rules: do not follow symlinks or junctions. Skip folders whose names start with `.` or `$`, and skip `System Volume Information`. Skip the library folder if it lies inside the source.
- Each image stores: relative path, size, modified time, width and height, format, `taken_at_local`, and `date_source`.
- **Date:** EXIF `DateTimeOriginal`, falling back to the file's modified time (converted to local time). `date_source` is `exif` or `mtime`. Filename dates and Takeout come in V1.
- Workers read metadata and generate the thumbnail in one pass. There is no hashing in V0.
- Scanning runs in the background with progress (files processed out of files found) and a Cancel button. Cancel and resume work as described in section 6.3.
- After each scan, a summary shows: added, updated, unchanged, missing, skipped (by extension), and errors (unreadable files). The same counts are stored in `scan_runs`.
- Disappeared files are marked `missing` and hidden from the grid. A missing file that reappears with the same path goes back to `ok`. V0 has no "remove missing" action, because changing the folder covers that.

### 7.3 Browsing

- **Grid:** a standard QListView in icon mode with no month headers. It is sorted newest first by `taken_at_local`, with ties broken by relative path. Thumbnails load lazily for the visible area (section 6.2).
- **Grid during a scan:** photos appear as each batch is committed, inserted at their place in grid order. When the scan ends, the grid reloads (hiding missing files) and keeps the photo you were looking at in view.
- **Viewer:** opens on double-click or Enter. It shows the image fitted to the window with EXIF orientation applied. Left and Right move to the previous and next photo in grid order. A **Back to photos** button, Esc, Backspace, or the mouse back button returns to the grid. A status line shows the filename, dimensions, date, and date source. Zoom, pan, and the info panel come in V1.
- **Help menu:** "Open log folder" (section 6.6).

### 7.4 Out of V0

Hashing, duplicate detection, moved-file detection, Takeout parsing, filename dates, albums, search and filters, multiple sources, month headers, the custom virtualized grid, viewer zoom and pan, the library folder setting, and the zipped folder build.

### 7.5 V0 is done when

1. Unit tests pass for the walk rules, the scan diff, the date fallback, and migrations (creating a new database at the current version).
2. An integration test scans a fixture folder and checks the rows. A second scan with no changes processes 0 files. After a file is modified and another is deleted, a rescan reports 1 updated and 1 missing.
3. Cancelling mid-scan and then rescanning finishes with no duplicate rows and does not reprocess files that were already committed.
4. A corrupt image is counted as an error and the scan continues.
5. A test confirms that the source folder is unchanged after a scan: the same file list, sizes, and modified times, and no new files.
6. The 100,000-photo fake library (section 10) scans to completion. An unchanged rescan takes under 1 minute, the first screen of thumbnails appears within 1 second of relaunch, and scrolling is recorded as smooth or not. The results are written down.
7. The PyInstaller `--onefile` .exe starts on Windows 10 and Windows 11 and can scan a folder (this checks `freeze_support`). The .exe size is measured and recorded.

## 8. V1 requirements

### 8.1 Sources

- A **source** is a folder the user adds and gives a label (for example "Account A Takeout" or "Phone backup"). Multiple sources live in one library.
- **Type detection:** a folder is detected as Takeout if it, or a direct child, is named "Google Photos" (or its localized name), or if JSON sidecars sit next to a large share of its images. The "Add source" dialog shows the detected type, and the user can change it. Changing the type later recomputes dates for that source.
- **Relocate source:** if a drive letter changes or a folder is moved, the user points the source to its new location. Only `root_path` changes, because file paths are stored relative to the source root.
- **Remove source:** after confirmation, deletes that source's rows from the database. Photos left with no files are removed along with their album memberships. Files on disk are never touched.

### 8.2 Scanning, rescanning, and missing files

- Recursive scan for JPEG, PNG, GIF, and HEIC. RAW, video, and other files are skipped and counted by extension in the scan summary.
- Incremental rescans: files with unchanged size and modified time are not reprocessed.
- Scans run in the background with progress (files processed out of files found) and cancel. Resume works as described in section 6.3.
- **Disappeared files** are marked `missing` and their rows are kept, because an unplugged drive looks the same as deleted photos.
- **Moved files:** if, in the same scan, a disappeared file and a new file in the same source have the same hash, the existing row's path is updated instead of creating a new row. Albums are unaffected either way, because they reference photos, not files.
- **Missing photos:** a photo is missing only when all of its files are missing. Missing photos are hidden from the grid by default and can be shown with a "Missing" filter.
- **Remove missing items:** a manual, confirmed action per source deletes that source's missing file rows. Photos left with no files are removed along with their album memberships. Never automatic.

### 8.3 Takeout import (folders)

- Reads extracted Takeout folders (zips are V1.5).
- **Sidecar matching** must tolerate Takeout's naming quirks:
  - the plain form `IMG_1234.jpg.json`;
  - the newer form `IMG_1234.jpg.supplemental-metadata.json`, which is itself often truncated (for example `.supplemental-metad.json`);
  - long names truncated to about 46 to 51 characters before the extension;
  - numbered duplicates where the counter moves, such as `IMG_1234(1).jpg` matching `IMG_1234.jpg(1).json`;
  - edited copies such as `IMG_1234-edited.jpg`, which have no own sidecar and use the original's.
- Fields read from the sidecar: `photoTakenTime`, `geoData` (ignored if both coordinates are 0), and `title` (the original filename, used to confirm a match).
- **Folders:** the `Trash` folder is skipped by default. `Archive` is imported normally. Takeout album folders are scanned like any other folder; their copies of photos from the year folders collapse as exact duplicates, and no Imagory album is created from them.
- **Edited copies** are separate photos. Both appear in the grid, and the edited copy gets its date from the original's sidecar.
- A sidecar matching report (matched, unmatched images, unused sidecars) is written to the log after each Takeout scan, to tune the matching rules.

### 8.4 Dates

Each file gets one best date and a `date_source` recording where it came from (`takeout_json`, `exif`, `filename`, or `mtime`), so wrong results can be debugged.

**Priority (to validate on real exports):**

- Takeout sources: Takeout JSON, then EXIF, then filename, then file modified time.
- Plain folders: EXIF, then filename, then file modified time.

**Time zones.** Takeout JSON gives an exact UTC moment, while EXIF gives local wall-clock time with no offset. Sorting mixed sources needs one consistent value, so:

- `taken_at_local` (wall-clock time as the photographer saw it) is used for sorting, grouping, and display.
- `taken_at_utc` is stored when known (from the JSON, or EXIF with `OffsetTimeOriginal`).
- For Takeout files with both values: if the EXIF time and the JSON time differ by less than 24 hours, the difference is assumed to be a time zone offset, and the EXIF wall-clock time is used as `taken_at_local` with the JSON UTC time kept. Otherwise the JSON time wins, converted with the computer's time zone.

**Filename patterns** recognized in V1: `IMG_20190131_235959`, `PXL_20190131_235959123`, `IMG-20190131-WA0001` (WhatsApp, date only), `Screenshot_20190131-235959`, and `2019-01-31 23.59.59`. Dates before 1990 or in the future are rejected.

**Photo date across copies:** a photo takes the best-ranked date among all its files. For example, a photo found in both a plain folder and a Takeout export uses the Takeout JSON date.

### 8.5 Duplicate detection (exact only)

- Exact duplicates are files with the same BLAKE2b hash of the full file contents. **The photo's identity is its hash:** all files with the same hash belong to one photo.
- Duplicates are **hidden, not deleted.** The grid shows one photo, and the viewer shows an "Also found in..." list of every copy with its source and path.
- **Primary copy:** the first file that is not missing, ordered by source added date, then by first-seen date. If the primary copy goes missing, the next copy takes over automatically.
- A **"Has duplicates"** filter shows only photos with more than one copy.
- Known limit: Takeout can rewrite metadata inside files, so the same original may appear with different hashes and not be detected. Near-duplicate detection is future work.

### 8.6 Browsing

- **Grid:** sorted newest first by `taken_at_local`, with month section headers ("October 2026"). Photos with no date at all sort into an "Unknown date" section at the end.
- The grid is a custom virtualized view (based on QAbstractScrollArea). It computes row positions from per-month photo counts and only paints and loads the visible rows. This keeps scrolling smooth at 100,000 photos with headers, which a standard QListView does not handle well. Prototype this early in V1 (risk in section 17).
- **Selection:** click, Ctrl-click, Shift-click, and Ctrl+A. The right-click menu and toolbar offer "Add to album" and "Open in viewer".
- **Viewer:** fit-to-window view, zoom and pan, next and previous (arrow keys), and an info panel with date and its source, location coordinates if known, source label, full path, size, dimensions, and duplicate locations. "Show in Explorer" opens the folder containing the file (read-only, allowed by principle 1).

### 8.7 Search and filters

- **Text search** on filename and relative folder path (SQL `LIKE`, which is fast enough at this scale).
- **Filters:** date range, source, folder, album, "Has duplicates", and "Missing".
- Filters combine with AND, and the grid and month headers update to the filtered set.
- No person, object, or content search in V1.

### 8.8 Albums

- Manual albums only. Photos are added by reference, and no files are copied.
- Create, rename, and delete albums. Add and remove photos, including multi-selection. Deleting an album never touches the photos.
- An album is shown in the same grid, using the same month grouping.

## 9. Data model (SQLite)

The exact columns are finalized during implementation. The structure below is agreed.

```
sources      id, label, root_path, type ('plain' | 'takeout'), type_overridden,
             added_at, last_scan_at

photos       id, content_hash UNIQUE (NULL in V0), primary_file_id,
             width, height, format,
             taken_at_local, taken_at_utc, date_source,
             latitude, longitude

files        id, source_id, photo_id, relative_path, size, modified_time,
             content_hash, status ('ok' | 'missing'),
             taken_at_local, taken_at_utc, date_source,    -- this file's best date
             sidecar_relative_path,                        -- Takeout only
             first_seen_at, last_seen_at
             UNIQUE (source_id, relative_path)

albums       id, name, created_at
album_photos album_id, photo_id, added_at    PRIMARY KEY (album_id, photo_id)

scan_runs    id, source_id, started_at, finished_at, status,
             found, added, updated, unchanged, missing, errors,
             skipped_by_ext (JSON text)

settings     key, value
```

**Indexes:** `photos(taken_at_local)`, `photos(primary_file_id)`, `files(photo_id)`, `files(content_hash)`, `files(source_id, status)`, `scan_runs(source_id)`. The `photos(primary_file_id)` and `scan_runs(source_id)` indexes keep foreign key checks fast when many files or a source are deleted.

**Design choices**

- Albums reference `photos`, and a photo is identified by its hash, so moves, renames, and rescans never break albums.
- Per-file dates are stored, and each photo's date is derived from its files' dates by the priority rules. Changing a rule only requires recomputing, not rescanning.
- Paths are stored relative to the source root, with `/` as the separator and the case as found on disk. `relative_path` uses `COLLATE NOCASE` because Windows paths are case-insensitive. NOCASE only folds ASCII letters, which is an accepted limitation.
- `modified_time` is stored as integer nanoseconds (`st_mtime_ns`), so the scan diff compares exact values.
- `taken_at_local` is stored as ISO 8601 text with no offset (`2019-01-31T23:59:59`). `taken_at_utc` and the other timestamps (`added_at`, `first_seen_at`, and so on) are stored as UTC ISO 8601 text ending in `Z`. Both sort correctly as text.
- `photos.primary_file_id` is nullable and is set after the file row is inserted, which breaks the circular reference with `files.photo_id`.
- **V0 to V1 migration:** the migration changes only the schema. Files with an empty `content_hash` count as changed on the next scan. Once they are hashed, they are reattached to photos by hash, which merges duplicates, and V0 photos left with no files are deleted. V0 has no albums, so nothing else depends on V0 photo ids.
- Connection settings: `journal_mode=WAL`, `foreign_keys=ON`, `synchronous=NORMAL`.
- Schema version lives in `PRAGMA user_version`. Migrations are numbered functions that run in order inside a transaction, and the database file is copied to `imagory.db.bak` before migrating.
- Face data (V2) will be added by migration: a `faces` table with photo_id, bounding box, embedding BLOB, model name, and model version, plus a `people` table.

## 10. Performance targets

- Design target: **100,000 photos** on a mid-range laptop with an SSD.
- Grid: smooth scrolling at 100,000 photos; first screen of thumbnails visible within 1 second of opening an already-scanned library.
- Rescan of an unchanged 100,000-photo library (walk and diff only): under 1 minute on an SSD.
- Initial scan (hash and thumbnail for every file) may take tens of minutes to an hour depending on the drive. This is acceptable because it runs in the background with progress and resumes after interruption.
- The UI stays responsive during scans. CPU-heavy work runs in worker processes, not threads, because Python threads do not run CPU work in parallel.
- **Testing:** a script generates a fake library (100,000 small images with varied EXIF dates, a share of exact duplicates, and a synthetic Takeout layout with JSON sidecars using all naming quirks from section 8.3). Real photos are tested by the author locally and are never committed to the repository. Run the 100,000-photo test at the end of V0 (scan and plain grid) and again when the V1 grid prototype exists.

## 11. Privacy

- Fully offline in V0 and V1: no telemetry, no accounts, no network access, no update checks.
- All data stays on the user's machine. Logs are local files only.
- State this prominently in the README.

## 12. Testing

- Unit tests (pytest) for sidecar matching, filename date parsing, date priority and time zone rules, primary copy selection, the scan diff, and migrations.
- Integration tests scan small generated fixture folders (plain and Takeout) into a temporary library and check the resulting database.
- The fake library generator from section 10 is part of the repository.

## 13. Packaging and distribution

- **V0 and V1 ship a single-file .exe** built with PyInstaller (`--onefile`).
- **A zipped folder build** (`--onedir`, still launched by double-clicking `Imagory.exe`) is added later as a second download, built from the same spec file. It starts faster and triggers fewer antivirus warnings.
- To keep both builds working without code changes: locate bundled resources through a single helper that handles PyInstaller's temporary folder (`sys._MEIPASS`) and normal runs, and never write data next to the executable.
- Known single-file trade-offs: startup of a few seconds while it unpacks, and more antivirus false positives.
- Published on GitHub releases. Free, unsigned at first. The README warns that Windows SmartScreen or antivirus may show warnings, and explains how to proceed.
- **Size expectation:** roughly 30 to 60 MB, reducible to roughly 25 to 40 MB with a clean virtual environment, excluded modules, and unneeded Qt plugins removed. Measure at the end of V0.
- **Licensing:** all Imagory code is released under the **MIT licence** (a `LICENSE` file in the repository root, and the licence shown in the app's About dialog).
- **Third-party components** bundled in the .exe keep their own licences, which MIT does not replace. The release includes a `THIRD_PARTY_LICENSES.txt` with their licence texts. Main ones: PySide6/Qt (LGPL; because Imagory's source and build instructions are public, users can rebuild with a different Qt version, which is the usual way to comply for a single-file build), Pillow (MIT-CMU), and pillow-heif with its bundled codecs. Verify the pillow-heif codec licences before the first release (section 18). Only dependencies with permissive or LGPL licences are added, so the app can stay MIT.

## 14. Proposed future work

**V1.5, Takeout zips.** Support multi-part zip exports, where a photo and its JSON sidecar can land in different parts. The approach (read inside zips, or extract into an app-managed folder) is undecided.

**V2, faces as an extension.**

- Face detection and recognition ship as a separate helper program, downloaded on demand, so the main app stays small. A frozen app cannot easily load new Python packages at runtime.
- The main app launches the helper once per scan and passes file paths, never once per photo. Results return in batches through the shared database or a pipe.
- Candidate models: YuNet (detection) and SFace (recognition), run through OpenCV. Check model licences before bundling or hosting them.
- Each face is stored as an embedding (a list of numbers produced by a trained neural network), so faces can be clustered into people without re-running the slow model. Store the model name and version, because embeddings from different models are not compatible.
- The UI needs merge, split, and rename for people, because clustering is never perfect.
- A first full scan of tens of thousands of photos may take hours. Support pause and resume.

**Later:** portable mode (marker file next to the .exe), near-duplicate detection with perceptual hashing (suggest, never hide automatically), Takeout album import, hiding originals behind edited copies, optional export of organized copies, an installer, additional formats.

## 15. Competitive context

| Project | Notes |
| --- | --- |
| Immich | Dominant self-hosted Google Photos alternative with face recognition. Server and Docker based. An import tool called immich-go handles Takeout. |
| PhotoPrism | Self-hosted with face recognition. Server-based. |
| digiKam | Free desktop app on Windows, Linux, and macOS with face recognition, written in C++ and Qt, GPL-licensed. Official repository at invent.kde.org/graphics/digikam. Its Takeout handling was not verified. |

**Gap Imagory targets:** a simple Windows app with Takeout-aware import and no server setup. Test digiKam and Immich on a real Takeout sample to confirm the gap before investing heavily in V1.

Do not copy digiKam code. It is GPL-licensed and Imagory is MIT.

## 16. Decisions log

| Topic | Decision |
| --- | --- |
| Audience | Non-technical users, open source; personal use for now |
| File handling | Read-only, store paths only, no app data in photo folders |
| V0 | Directory scan, preview, SQLite storage, using the V1 core schema |
| V0 behaviour | Reopen from database, manual incremental rescan; changing the folder replaces the source after confirmation; newest first by EXIF date, falling back to modified time |
| V1 | Sources, Takeout folders, exact duplicates, grid, viewer, search and filters, albums |
| Takeout zips | V1.5 |
| Architecture | One database writer thread, worker processes for CPU work, commit per batch |
| Photo identity | Content hash (BLAKE2b); albums reference photos |
| Duplicates | Exact hash, hidden not deleted, "Has duplicates" filter |
| Primary copy | First non-missing file by source added date, then first seen |
| Missing files | Mark missing, keep rows, detect moves by hash, manual per-source removal |
| Dates | Per-file best date with date_source; local wall-clock for sorting, UTC kept when known |
| Takeout folders | Skip Trash, import Archive, album folders collapse as duplicates |
| Edited copies | Separate photos, date from the original's sidecar |
| Source type | Auto-detected, user can override |
| Grid | Newest first with month headers, custom virtualized view |
| Selection | Multi-select (Ctrl, Shift, Ctrl+A) |
| Formats | JPEG, PNG, GIF, HEIC. No RAW, no video |
| Search | Filename and path text; date, source, folder, album, duplicates, missing filters |
| Library location | %LOCALAPPDATA%\Imagory, user can change; no portable mode in V1 |
| Privacy | Fully offline in V0 and V1 |
| Platform | Windows 10 and 11, Python 3.12, PySide6 |
| Output | Single .exe first, zipped folder build later |
| License | MIT for all Imagory code; bundled dependencies keep their own licences, listed in THIRD_PARTY_LICENSES.txt |
| Name | Imagory |
| Qt package | `pyside6-essentials` (LGPL) instead of the full `pyside6` meta-package, to keep the build small |
| V0 unreadable files | Not stored in the database; counted as errors and retried on every scan, so a repaired file is picked up |
| V0 missing files | Marked right after the walk and diff, before processing, so a scan cancelled during processing still records them; "missing" in the summary counts newly missing files |
| Stored dimensions | Width and height as displayed, after EXIF orientation is applied |
| Worker processes | Started with the `spawn` method on every platform, matching Windows |
| Grid painting | Custom item delegate paints prepared pixmaps directly; thumbnails decode in C++ (`QImageReader`) on two loader threads to limit GIL contention with the UI |
| Grid loading | The first 500 rows are queried directly (index-ordered, so the LIMIT stops early) and the rest load on a background read-only thread, to meet the 1-second first screen at 100,000 photos |
| Grid during a scan | The scan coordinator reports the grid rows of each committed batch to the UI (a signal, no UI database access); the grid inserts them in sorted order. A 200-photo batch costs about 45 ms of UI time on a 100,000-photo grid |
| Leaving the viewer | A visible Back button, because Esc alone was not discoverable (found testing the Windows .exe) |
| Sources (V1) | Several sources in one library and one grid; one scan thread scans the chosen sources in turn, and a source whose folder is unavailable is reported while the others are still scanned |
| Thumbnail layout (V1) | One folder per source (`thumbs/source-<id>/`), so removing a source deletes its thumbnails in one step; a cache written with another layout is cleared once on startup (tracked by the `thumbs_layout` setting). Keyed by content hash once section 8.5 lands |
| Takeout detection | Folder names only, no file opens: a "Google Photos" folder (or localized name) at or directly under the root, or sidecar names next to at least half of a sample of images |
| Adding a source | Refused if the folder is already a source or is inside the library folder; an overlapping folder is allowed after a warning |
| Relocate check | A sample of 50 known relative paths is checked under the new folder, and the user confirms if few are found |
| Remove source | Thumbnails are deleted first (they regenerate if removal stops halfway), then the rows; photos left with no files go too. Removing album memberships waits for section 8.8 |

## 17. Risks

- **Takeout complexity:** inconsistent filenames, sidecar mismatches, and rewritten metadata may cause incorrect dates or missed duplicates. Mitigated by tolerant matching, the matching report, and date_source.
- **Grid with month headers:** a custom virtualized view is the most complex UI part. Prototype it early with the 100,000-photo fake library; fall back to a flat grid if it blocks V1.
- **Executable size and antivirus:** single-file PyInstaller builds are large and often flagged when unsigned. Mitigated later by the folder build.
- **Scale:** scan time and grid smoothness at 100,000 photos need measurement.
- **Database safety:** cloud-synced folders can corrupt SQLite. Mitigated by the warning and the pre-migration backup.
- **Licensing:** pillow-heif codec licences and LGPL obligations for PySide6 must be checked before release. Face model weights (V2) may restrict use.
- **Scope creep:** faces, zips, and portable mode stay out of V0 and V1.
- **Duplicate limits:** exact-hash detection misses edited or re-saved copies, and users may expect more.

## 18. Open questions

1. **Date priority:** validate the rules in section 8.4 on a real Takeout export.
2. **pillow-heif licences:** the pillow-heif 1.8 wheels bundle libheif and libde265 (LGPLv3) and **x265 (GPLv2)**, and state that the binary wheels are GPLv2 as a whole (the Windows wheels add the MinGW runtime). Distributing the .exe with them would make the bundled binary GPLv2, which conflicts with the permissive/LGPL-only dependency rule. Options: accept GPLv2 for the binary, build a decode-only libheif without x265, or drop HEIC. Decide before the first public release.
3. **Competitor check:** test digiKam and Immich on a real Takeout export before building V1.
4. **Zip handling (V1.5):** read inside zips or extract to an app-managed folder.
5. **V2 details:** confirm faces as a downloadable extension and its position on the roadmap.
6. **Localized Takeout folder names (V1):** collect the localized names of "Google Photos" that type detection should recognize.
