# Imagory: Product Requirements Document

Version 1.2 · October 31, 2026 · Status: V0 implemented (Windows checks pending, section 7.5). V1 in progress: sources (8.1), duplicates, moves and missing files (8.5, 8.2), plain-folder dates (8.4), the virtualized grid (8.6), and the album and filter layers (8.7, 8.8) implemented; Takeout sidecars are matched but not yet used by the scan, and the filter bar and albums are not yet in the main window. Remaining open items are listed in section 18.

## 1. Overview

Imagory is a free, open source, fully offline Windows application that helps non-technical people browse and organize photo collections stored in local folders, including Google Photos exports (Google Takeout). It indexes photos into a local SQLite database, shows them in a fast thumbnail grid and viewer, detects exact duplicates, and lets users search, filter, and group photos into albums. It never modifies, moves, or deletes the user's original files.

**Problem:** People who leave Google Photos end up with Takeout exports where metadata sits in separate JSON files, dates are often wrong, and duplicates are common. Existing open source options (Immich, PhotoPrism) are server-based and need Docker or technical setup. digiKam is a full desktop app but is feature-dense and, as far as research found, does not obviously repair Takeout metadata (unverified).

**Positioning:** A simple Windows app: point it at folders, get a clean browsable library.

## 2. Target users

- **Primary (eventually):** non-technical Windows users with photos in local folders or a Google Takeout export.
- **Right now:** the author, for personal use. Open source from the start.

## 3. Principles and non-goals

**Principles**

1. **Read-only on originals.** The app only reads photo files and stores their paths. It never moves, edits, renames, or deletes them, and never writes app data into photo folders. One exception: a folder that Imagory itself created as a Takeout cleanup output (section 8.10) holds Imagory's copies and a `.imagory/` folder (database, thumbnails, `metadata.json`, reports). The cleanup's input is read-only like every other source.
2. **Fully offline.** No telemetry, accounts, update checks, or network calls in V0 and V1.
3. **Disposable cache, durable library.** Thumbnails can be deleted and regenerated at any time. The database is the source of truth for Imagory's own data (sources, albums, dates).
4. **Hide, never destroy.** Duplicates are hidden from view, not deleted. The database is never purged automatically; removal of database rows only happens through an explicit, confirmed user action.

**Non-goals (V0 and V1)**

- Deleting or moving files on disk, including duplicate removal. Takeout cleanup (section 8.10) writes deduplicated **copies** to a folder the user picks; it never deletes or moves the originals, and deleting the Takeout afterwards is left to the user.
- Face detection and recognition (planned for V2). Reading the person names that Google already stored in Takeout sidecars is not face recognition and is in scope (section 8.9).
- RAW and video support. Takeout cleanup copies RAW files and videos as-is so nothing is lost, but they are not shown in the grid.
- Cloud sync, mobile apps, multi-user, accounts.
- Photo editing.
- Near-duplicate (visually similar) detection.
- Map view.
- Portable mode (data stored next to the .exe).
- Importing Takeout albums as Imagory albums, except through Takeout cleanup (section 8.10), where album folders and Archive become albums.

## 4. Platform and technology

| Item | Decision |
| --- | --- |
| Platform | Windows 10 and Windows 11 (64-bit), the main target. Linux x86_64 as a best-effort second build from 0.2 (section 13) |
| Language | Python 3.12 |
| Project and dependency management | uv (`pyproject.toml` plus a committed `uv.lock`) |
| UI framework | PySide6 (Qt 6) |
| Storage | SQLite (Python built-in sqlite3) |
| Image reading | Pillow, plus pillow-heif for HEIC |
| Hashing | BLAKE2b from Python hashlib (built in, fast), read in 1 MB chunks |
| Packaging | PyInstaller. Single-file .exe first, zipped folder build added later; a single-file Linux binary in a `.tar.gz` from the same spec (section 13) |
| Tests | pytest |
| License | MIT |
| Distribution | GitHub releases, no payment, no code signing |
| Other platforms | macOS and Linux ARM are not in scope; avoid platform-only code where it costs nothing |

## 5. Roadmap

| Version | Scope | Status |
| --- | --- | --- |
| **V0** | Pick a directory, scan it, preview images, store image data in SQLite | Confirmed |
| **V1** | Multiple sources, Takeout folder import, exact duplicate detection, month-grouped thumbnail grid, photo viewer, search and filters, manual albums | Confirmed |
| **V1.x** | Zipped folder build published alongside the single .exe | Confirmed, timing flexible |
| **V1.x** | Takeout cleanup: a deduplicated, metadata-preserving copy of an extracted Takeout (section 8.10) | Confirmed, planned |
| **V1.x** | Editable people: rename, merge, add or remove photos, delete (section 14) | Proposed, **needs planning** |
| **V1.5** | Takeout import from .zip files, including multi-part exports | Confirmed as after V1 |
| **V2** | Face detection and recognition as a separate downloadable extension | Proposed, details not confirmed |

## 6. Architecture

### 6.1 Components

The code is split into small modules with one job each. The UI never touches files or the database directly during a scan.

| Module | Responsibility |
| --- | --- |
| `db` | A package holding connection setup (WAL, foreign keys), the schema and migrations, and all SQL queries, one module per area; `imagory.db` re-exports every public name |
| `scanner` | Walks a source, compares it to the database, queues work, applies results |
| `metadata` | Reads dimensions, format, EXIF date, GPS, orientation; parses dates from filenames |
| `takeout` | Detects Takeout sources, finds and parses JSON sidecars |
| `thumbs` | Generates and locates cached thumbnails |
| `cleanup` | Takeout cleanup (section 8.10), a package: folder checks, inventory, pure planning, hashing, verified copy, export, and the pipeline that runs the stages |
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
4. **Apply** results: insert or update file rows, attach each file to a photo by its hash, and recompute the photo's primary file and date.
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
- V0 keyed thumbnails by a hash of the relative path. V1 keys them by content hash. The cache is cleared once when its layout version changes, and it rebuilds on demand.

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
  - edited files such as `IMG_1234-edited.jpg`, which have no own sidecar and use the original's.
- Fields read from the sidecar: `photoTakenTime`, `geoData` (ignored if both coordinates are 0), and `title` (the original filename, used to confirm a match), plus, from the people-and-metadata task (section 8.9): `people` (the names tagged in Google Photos), `description` (the caption), `favorited`, and `creationTime` (when the photo was added to Google Photos). Other fields (`url`, `imageViews`, device and app info) are ignored: `url` is a Google link, and the rest is noise for an offline app.
- **Folders:** the `Trash` folder is skipped by default. `Archive` is imported normally. Takeout album folders are scanned like any other folder; the photos in them that also sit in the year folders collapse as exact duplicates, and no Imagory album is created from them.
- **Edited files** are separate photos. Both appear in the grid, and the edited file gets its date from the original's sidecar.
- A sidecar matching report (matched, unmatched images, unused sidecars) is written to the log after each Takeout scan, to tune the matching rules.

### 8.4 Dates

Each file gets one best date and a `date_source` recording where it came from (`takeout_json`, `exif`, `filename`, or `mtime`), so wrong results can be debugged.

**Priority (to validate on real exports):**

- Takeout sources: Takeout JSON, then EXIF, then filename, then file modified time.
- Plain folders: EXIF, then filename, then file modified time.
- Takeout cleanup (section 8.10), and files in a `cleaned` source that are confirmed in its output database (same path and hash): Takeout JSON, then EXIF, then filename. **The file modified time is never used for dates that come from a Takeout**, because there it is the time the zip was extracted. Other files in a `cleaned` source (for example added there by hand) follow the plain-folder rules, modified time included. A file with none of the three stays undated (`taken_at_local`, `taken_at_utc` and `date_source` are NULL) and appears under "Unknown date". Videos use the sidecar date, then the filename date; dates inside video containers are not read.

**Time zones.** Takeout JSON gives an exact UTC moment, while EXIF gives local wall-clock time with no offset. Sorting mixed sources needs one consistent value, so:

- `taken_at_local` (wall-clock time as the photographer saw it) is used for sorting, grouping, and display.
- `taken_at_utc` is stored when known (from the JSON, or EXIF with `OffsetTimeOriginal`).
- For Takeout files with both values: if the EXIF time and the JSON time differ by less than 24 hours, the difference is assumed to be a time zone offset, and the EXIF wall-clock time is used as `taken_at_local` with the JSON UTC time kept. Otherwise the JSON time wins, converted with the computer's time zone.

**Filename patterns** recognized in V1: `IMG_20190131_235959`, `VID_20190131_235959` (Android video), `PXL_20190131_235959123`, `IMG-20190131-WA0001` (WhatsApp, date only), `Screenshot_20190131-235959`, and `2019-01-31 23.59.59`. Dates before 1990 or in the future are rejected.

**Photo date across files:** a photo takes the best-ranked date among all its files. For example, a photo found in both a plain folder and a Takeout export uses the Takeout JSON date.

### 8.5 Duplicate detection (exact only)

- Exact duplicates are files with the same BLAKE2b hash of the full file contents. **The photo's identity is its hash:** all files with the same hash belong to one photo.
- Duplicates are **hidden, not deleted.** The grid shows one photo, and the viewer shows an "Also found in..." list of every file with its source and path.
- **Primary file:** the first file that is not missing, ordered by source added date, then by first-seen date. If the primary file goes missing, the next file takes over automatically.
- A **"Has duplicates"** filter shows only photos with more than one file.
- Known limit: Takeout can rewrite metadata inside files, so the same original may appear with different hashes and not be detected. Near-duplicate detection is future work.

### 8.6 Browsing

- **Grid:** sorted newest first by `taken_at_local`, with month section headers ("October 2026"). Photos with no date at all sort into an "Unknown date" section at the end.
- The grid is a custom virtualized view (based on QAbstractScrollArea). It computes row positions from per-month photo counts and only paints and loads the visible rows. This keeps scrolling smooth at 100,000 photos with headers, which a standard QListView does not handle well. Prototype this early in V1 (risk in section 17).
- **Selection:** click, Ctrl-click, Shift-click, and Ctrl+A. The right-click menu and toolbar offer "Add to album" and "Open in viewer".
- **Viewer:** fit-to-window view, zoom and pan, next and previous (arrow keys), and an info panel with date and its source, location coordinates if known, source label, full path, size, dimensions, duplicate locations, and, for Takeout photos, the people, caption, favourite flag, and Google Photos upload date. "Show in Explorer" opens the folder containing the file (read-only, allowed by principle 1).

### 8.7 Search and filters

- **Text search** on filename, relative folder path, person names, and the caption (SQL `LIKE`, which is fast enough at this scale). Every word must match at least one of these.
- **Filters:** date range, source, folder, album, person, "Has duplicates", and "Missing".
- Filters combine with AND, and the grid and month headers update to the filtered set.
- No object or content search, and no face-based search, in V1. Person search and filtering use only the names from Takeout sidecars.

### 8.8 Albums

- Manual albums only. Photos are added by reference, and no files are copied.
- Create, rename, and delete albums. Add and remove photos, including multi-selection. Deleting an album never touches the photos.
- An album is shown in the same grid, using the same month grouping.

### 8.9 People and Takeout metadata

Google Photos stores the names you tagged in a photo in its Takeout sidecar (`people`). Imagory keeps them. This is not face recognition: Imagory only copies text that is already in the sidecar.

- **Reading:** `takeout.parse_sidecar` also returns the people names, caption, favourite flag, and upload time. Names are trimmed, blank ones dropped, and duplicates (ignoring case) collapsed.
- **Storing:** a `people` table and a `photo_people` link table (section 9). People are linked to photos, not files, so a photo that exists in several folders or sources has the **union** of the names from all its sidecars. Edited files take the original's people, as they do for dates and location.
- **Never removed automatically** (principle 4): a rescan only adds names. If a name was removed in Google Photos before the export, a photo scanned earlier keeps the old name; this is accepted for now.
- **Caption and favourite:** the first non-empty caption is kept; a photo is a favourite if any of its files is.
- **Existing libraries:** no backfill. A normal scan skips unchanged files, so photos scanned before this feature get names only when they are scanned again. While Imagory is in development, remove the Takeout source (or delete the library folder) and add it again.
- **People section:** the sidebar gets a "People" section of its own (like Albums and Sources, with a clickable header that collapses it; closed by default) listing each person with their photo count, most photos first. Clicking a person opens the usual grid and month headers filtered to that person (combined with AND with the other filters, like an album). Only people with at least one visible photo are listed. With none, the open section shows "No people yet". Albums and Sources collapse the same way. Disabled during scans, like albums.
- **Filter bar:** a person control sets the same filter and shows as a chip.
- **Search:** the text box also matches person names and captions.
- **Out of scope:** renaming, merging, or deleting people, adding or removing a person's photos by hand (planned for V1.x, section 14), a Favourites filter, and anything face-based (V2).
- **Privacy:** names are personal data stored only in the local database. Nothing is sent anywhere.

### 8.10 Takeout cleanup

Make a clean copy of an extracted Google Takeout folder: one file per unique photo, no JSON sidecars, a `YYYY/MM` layout, and a database plus a `metadata.json` that keep all the Takeout metadata. Afterwards the user can delete the original Takeout by hand and lose nothing. The full plan, with every rule and the pipeline, is `docs/takeout-cleanup.md`; this section is the agreed summary.

- **Originals:** opened read-only, never modified, moved, renamed or deleted (principle 1). Tests assert the input is unchanged.
- **Entry point:** a **Clean up Takeout…** toolbar button next to Add source and in the File menu. It opens a non-modal six-page wizard: input, output, analyse, preview, copy, done. Disabled during scans; scans, Add source, source and album actions are disabled during a cleanup (browsing, the people filter and settings stay available, because a cleanup never writes the main library). Opening it again raises the open wizard. Each job (analysis, copy) runs on its own thread with its own pipeline, and its results go only to the wizard that started it.
- **Input and output:** one extracted Takeout folder per run (zips stay V1.5). The output is an empty folder, or an existing cleanup output to merge a later run into (for example a second account); a cancelled run resumes when the same input is picked again. Output and input must not contain each other or sit in the library folder, and an output that is already a `plain` or `takeout` source is refused. A cloud or network folder gets a warning, because the output holds SQLite.
- **Duplicates:** one file per content hash (section 8.5). A file already in the output wins, then a year folder, then a non-album folder, then the shortest path, then path order. Every other file is recorded as an origin of the kept file.
- **What is copied:** photos, videos, RAW and every other file except used sidecars and Trash. Images the scanner can't decode are copied as-is and listed. JSON files that aren't used sidecars go to `.imagory/takeout-json/<original path>`. A same-stem companion with no sidecar (live or motion photo pair) follows its photo's folder and name.
- **Layout and names:** `<output>/YYYY/MM/<name>` by best date (section 8.4 cleanup rule), or `<output>/Unknown date/`. The name is the sidecar title (the original name before Takeout truncated it) with the file's real extension, `-edited` copies keep their suffix, and the on-disk name is used without a sidecar. Names are sanitized for Windows, clashes get ` (1)`, ` (2)` (compared with casefold), and paths over 250 characters have their stem shortened. Every rename is in the report.
- **Copy safety:** each file is written to `<name>.part` while the source is hashed (it must match the analysis), then the `.part` is re-read and hashed, renamed into place, and its modified time set to the taken time (files with no date keep their original mtime). Free space (bytes plus about 5%) is checked before copying.
- **Output folder:** `<output>/.imagory/` holds `imagory.db` (the normal schema), `thumbs/`, `metadata.json` (a plain-text copy of all metadata, rewritten at the end of each run), `takeout-json/` and `reports/cleanup-<UTC timestamp>.txt`. Paths are relative to the output root, so the folder can be moved or renamed.
- **"Safe to delete":** shown only when every file in the input except Trash is accounted for (a verified copy, a duplicate of one, a stored sidecar, or a copied JSON file). Otherwise the unaccounted files are listed.
- **Albums:** each Takeout album folder (named by its album JSON title when present) and Archive becomes an album in the output database. Membership comes from every copy, including duplicates not copied. Both originals and `-edited` copies are kept.
- **Add to library:** the output becomes a source of type `cleaned` in the main library. A `cleaned` scan reads dates, people, caption, favourite, upload time and location from the output database (matched by path, confirmed by hash), keeps undated files undated, and falls back to the plain rules for files not in it. **Add source** detects a folder holding `.imagory/imagory.db` as `cleaned`; "Change type…" is not offered for it. The run's albums are created once, after the first completed scan of the `cleaned` source (and retried after every later completed scan, so a cancelled scan loses nothing); a name clash creates `<name> (Takeout)`, then `<name> (Takeout 2)`, and existing albums are never changed. If the input is already a source, the wizard offers (with confirmation, showing how many photos only that source has) to remove it once a scan of the cleaned source that started after the run finished has completed; plain Add source of an output can't offer this, because it doesn't know the input; photos keep their ids, albums and people because identity is the hash.
- **Threads:** a `CleanupThread` is the only writer of the output database; worker processes hash, read metadata and make thumbnails (section 6.2). The main library database is untouched until Add to library.
- **Out of scope:** deleting or moving originals, zips, writing EXIF or XMP into copies, near-duplicates, opening the output as its own library, rebuilding a database from `metadata.json`, and writing later main-library edits back to the output database.

## 9. Data model (SQLite)

The exact columns are finalized during implementation. The structure below is agreed.

```
sources      id, label, root_path, type ('plain' | 'takeout' | 'cleaned'), type_overridden,
             added_at, last_scan_at

photos       id, content_hash UNIQUE (NULL in V0), primary_file_id,
             width, height, format,
             taken_at_local, taken_at_utc, date_source,
             latitude, longitude,
             description, favorite (0 | 1), uploaded_at_utc     -- Takeout only

files        id, source_id, photo_id, relative_path, size, modified_time,
             content_hash, status ('ok' | 'missing'),
             exif_taken_at, exif_offset,                   -- raw EXIF inputs, for recomputing dates
             taken_at_local, taken_at_utc, date_source,    -- this file's best date
             sidecar_relative_path,                        -- Takeout only
             first_seen_at, last_seen_at
             UNIQUE (source_id, relative_path)

people       id, name UNIQUE COLLATE NOCASE
photo_people photo_id, person_id    PRIMARY KEY (photo_id, person_id)

albums       id, name, created_at
album_photos album_id, photo_id, added_at    PRIMARY KEY (album_id, photo_id)

scan_runs    id, source_id, started_at, finished_at, status,
             found, added, updated, unchanged, missing, moved, errors,
             skipped_by_ext (JSON text)

settings     key, value

-- Takeout cleanup (section 8.10). In every database, used only in output databases
cleanup_runs   id, input_path, input_label, started_at, finished_at,
               status ('analysing' | 'planned' | 'copying' | 'done' | 'cancelled'),
               photos, other_files, duplicates, trash_skipped, errors, bytes_copied
file_origins   id, run_id, file_id NULL, extra_file_id NULL,   -- exactly one set, the kept row
               original_relative_path, sidecar_relative_path, album_folder NULL
extra_files    id, source_id, relative_path, size, modified_time, content_hash, extension,
               taken_at_local, taken_at_utc, date_source, latitude, longitude,
               description, favorite, uploaded_at_utc, people (JSON text)
               UNIQUE (source_id, relative_path)
cleanup_copies id, run_id, file_id NULL, extra_file_id NULL, source_relative_path,
               target_relative_path UNIQUE COLLATE NOCASE,
               status ('pending' | 'done' | 'error'), error NULL
```

**Indexes:** `photos(taken_at_local)`, `photos(primary_file_id)`, `files(photo_id)`, `files(content_hash)`, `files(source_id, status)`, `files(source_id, content_hash)` (move detection), `album_photos(photo_id)`, `photo_people(person_id)`, `scan_runs(source_id)`, and for cleanup `file_origins(file_id)`, `file_origins(extra_file_id)`, `extra_files(content_hash)`, `cleanup_copies(run_id, status)`. The `photos(primary_file_id)` and `scan_runs(source_id)` indexes keep foreign key checks fast when many files or a source are deleted.

**Design choices**

- Albums reference `photos`, and a photo is identified by its hash, so moves, renames, and rescans never break albums.
- Per-file dates are stored, and each photo's date is derived from its files' dates by the priority rules. Changing a rule only requires recomputing, not rescanning.
- Paths are stored relative to the source root, with `/` as the separator and the case as found on disk. `relative_path` uses `COLLATE NOCASE` because Windows paths are case-insensitive. NOCASE only folds ASCII letters, which is an accepted limitation.
- `modified_time` is stored as integer nanoseconds (`st_mtime_ns`), so the scan diff compares exact values.
- `taken_at_local` is stored as ISO 8601 text with no offset (`2019-01-31T23:59:59`). `taken_at_utc` and the other timestamps (`added_at`, `first_seen_at`, and so on) are stored as UTC ISO 8601 text ending in `Z`. Both sort correctly as text.
- `photos.primary_file_id` is nullable and is set after the file row is inserted, which breaks the circular reference with `files.photo_id`.
- **One baseline schema.** No version has been released yet, so the schema is created directly at version 5 (the baseline) instead of by replaying development migrations. A database below the baseline comes from a development build and is deleted and recreated empty when Imagory starts; the user adds their folders again.
- Connection settings: `journal_mode=WAL`, `foreign_keys=ON`, `synchronous=NORMAL`.
- Schema version lives in `PRAGMA user_version`. Every change after the baseline is a numbered migration (6, 7, …) that runs inside a transaction, and the database file is copied to `imagory.db.bak` before migrating.
- `file_origins` rows always point at the kept file or extra-file row (the one that survives in the `cleaned` source); each input file's own path is in `original_relative_path`, so removing the temporary input source never loses provenance.
- Face data (V2) will be added by migration: a `faces` table with photo_id, bounding box, embedding BLOB, model name, and model version, plus a `people` table. The `people` table from section 8.9 is reused: V2 adds a face-to-person link rather than a second table.

## 10. Performance targets

- Design target: **100,000 photos** on a mid-range laptop with an SSD.
- Grid: smooth scrolling at 100,000 photos; first screen of thumbnails visible within 1 second of opening an already-scanned library.
- Rescan of an unchanged 100,000-photo library (walk and diff only): under 1 minute on an SSD.
- Initial scan (hash and thumbnail for every file) may take tens of minutes to an hour depending on the drive. This is acceptable because it runs in the background with progress and resumes after interruption.
- The UI stays responsive during scans. CPU-heavy work runs in worker processes, not threads, because Python threads do not run CPU work in parallel.
- **Testing:** a script generates a fake library (100,000 small images with varied EXIF dates, a share of exact duplicates, and a synthetic Takeout layout with JSON sidecars using all naming quirks from section 8.3). Real photos are tested by the author locally and are never committed to the repository. Run the 100,000-photo test at the end of V0 (scan and plain grid) and again when the V1 grid prototype exists.
- **Takeout fixture:** `scripts/make_fake_library.py --takeout` writes the synthetic export under `OUT/Takeout` and a ground-truth `OUT/manifest.json` (expected sidecar per image, raw EXIF, JSON and filename times, expected `date_source`, `taken_at_local`, `taken_at_utc` and location), used by the section 8.3 and 8.4 tests and to check the sidecar matching report.
- **V1 grid in the app** (`scripts/bench.py`, 95,000 photos after duplicates collapse, WSL2 offscreen, Ryzen 3 5300U; not Windows numbers): full scan 249 s, unchanged rescan 6.3 s, `month_counts` 27 ms, first screen of thumbnails 0.22 s after window creation (plus 0.3 to 0.44 s of Qt imports), whole grid loaded in the background 1.7 s, wheel scroll median 23.6 ms (11.6 ms with thumbnails in memory), one 200-photo scan batch inserted 42 ms. Filtered queries: first 200 rows in 5 to 205 ms depending on the filter. On Windows 11 (the .exe, 2026-10-31): 100,000 plain photos scanned in about 10 minutes and scrolling is smooth by eye; `bench.py` numbers on Windows are still to take.
- **Takeout cleanup** (`scripts/bench_cleanup.py SCRATCH --count 50000`, 2026-11-07, WSL2, 8 CPUs, 7 workers, little free memory; not Windows numbers; 50,000 instead of 100,000 by the author's choice): 49,517 images and 92,533 non-Trash files. Analyse photos 114 s (436 photos/s), other files 8 s, plan and preview 21 s, copy 162 s (47,288 copies, 292 per second; 162 to 170 s across runs), finalise 64 s: about 6 minutes in total, with 0 failed and 0 unaccounted. The 100,000-photo and Windows runs are still to do.
- **V1 grid prototype** (`scripts/grid_prototype.py --bench`, 100,000 in-memory items in 241 month sections, WSL offscreen): layout build 0.3 ms, reflow 0.04 ms, first paint 45 ms, full-viewport repaint about 1.5 ms (median scroll step 6 ms, worst 16 ms) with thumbnails in memory. Still to measure against the real thumbnail cache, by eye on a real display, and on Windows.

## 11. Privacy

- Fully offline in V0 and V1: no telemetry, no accounts, no network access, no update checks.
- All data stays on the user's machine. Logs are local files only.
- State this prominently in the README.

## 12. Testing

- Unit tests (pytest) for sidecar matching, filename date parsing, date priority and time zone rules, primary file selection, the scan diff, and migrations.
- Integration tests scan small generated fixture folders (plain and Takeout) into a temporary library and check the resulting database.
- The fake library generator from section 10 is part of the repository.

## 13. Packaging and distribution

- **V0 and V1 ship a single-file .exe** built with PyInstaller (`--onefile`).
- **A zipped folder build** (`--onedir`, still launched by double-clicking `Imagory.exe`) is added later as a second download, built from the same spec file. It starts faster and triggers fewer antivirus warnings.
- To keep both builds working without code changes: locate bundled resources through a single helper that handles PyInstaller's temporary folder (`sys._MEIPASS`) and normal runs, and never write data next to the executable.
- Known single-file trade-offs: startup of a few seconds while it unpacks, and more antivirus false positives.
- **Linux build (from 0.2, best effort):** the same spec builds a single-file `Imagory` binary on Linux, shipped as `Imagory-<version>-linux-x86_64.tar.gz` (with `LICENSE` and `THIRD_PARTY_LICENSES.txt`) so it keeps its executable bit. It is built on the oldest supported Ubuntu runner (22.04, glibc 2.35), because a PyInstaller binary runs only on systems with the same or a newer glibc. It needs a desktop with X11 or XWayland and the usual Qt system libraries (some distros need `libxcb-cursor0`). The library folder is `$XDG_DATA_HOME/imagory`; "Show in folder" opens the containing folder without selecting the file. No desktop entry, AppImage or Flatpak yet. Built and smoke-tested in CI (`.github/workflows/linux-build.yml`); tested by hand on Ubuntu and WSL only.
- **Website:** a static Astro site in `frontend/` at `https://imagory.mshahnawaz.me` (Vercel) shows what the app does, hosts user docs and a changelog, and links to the files on GitHub releases (Windows and Linux). Design and plan: `frontend/design.md`.
- Published on GitHub releases. Free, unsigned at first. The README warns that Windows SmartScreen or antivirus may show warnings, and explains how to proceed.
- **Size expectation:** roughly 30 to 60 MB, reducible to roughly 25 to 40 MB with a clean virtual environment, excluded modules, and unneeded Qt plugins removed. Measured on Windows 11 (V1, 2026-10-31): 49.4 MB.
- **Licensing:** all Imagory code is released under the **MIT licence** (a `LICENSE` file in the repository root, and the licence shown in the app's About dialog).
- **Third-party components** bundled in the .exe keep their own licences, which MIT does not replace. The release includes a `THIRD_PARTY_LICENSES.txt` with their licence texts. Main ones: PySide6/Qt (LGPL; because Imagory's source and build instructions are public, users can rebuild with a different Qt version, which is the usual way to comply for a single-file build), Pillow (MIT-CMU), and pillow-heif with its bundled codecs. Verify the pillow-heif codec licences before the first release (section 18). Only dependencies with permissive or LGPL licences are added, so the app can stay MIT.

## 14. Proposed future work

**V1.5, Takeout zips.** Support multi-part zip exports, where a photo and its JSON sidecar can land in different parts. The approach (read inside zips, or extract into an app-managed folder) is undecided.

**V1.x, editable people (needs planning).** V1 shows the people names read from Takeout and cannot change them (section 8.9). A later V1.x release should let the user correct them by hand. This is flagged as needing planning because the design below is only a first sketch, and nothing is decided.

- Wanted actions: rename a person, merge people who were wrongly split (for example "Ana" and "Anna"), add photos to or remove photos from a person, and delete a person.
- Manual edits must survive rescans. Today a scan only adds names from sidecars, so it would re-add a photo the user removed and recreate a name the user merged away.
- First sketch: mark each person-to-photo link as `takeout` or `manual`, record manually removed links so the scanner skips them, and keep an alias table (old name to person) so a merged name is not recreated. This needs a new numbered migration.
- The UI keeps the People sidebar section. Opening a person works like an album view, with add, remove, and merge actions. Albums stay a separate feature: storing people inside the albums tables was considered and rejected, because scans never touch albums, while they do touch people.
- Open questions: how a manual edit is undone, whether a merge can be reversed, and how this joins up with the merge, split, and rename UI that V2 needs (so it is built once).
- Deleting a person removes only the link rows, never photos, and needs a confirmation (principle 4).

**V2, faces as an extension.**

- Face detection and recognition ship as a separate helper program, downloaded on demand, so the main app stays small. A frozen app cannot easily load new Python packages at runtime.
- The main app launches the helper once per scan and passes file paths, never once per photo. Results return in batches through the shared database or a pipe.
- Candidate models: YuNet (detection) and SFace (recognition), run through OpenCV. Check model licences before bundling or hosting them.
- Each face is stored as an embedding (a list of numbers produced by a trained neural network), so faces can be clustered into people without re-running the slow model. Store the model name and version, because embeddings from different models are not compatible.
- The UI needs merge, split, and rename for people, because clustering is never perfect.
- A first full scan of tens of thousands of photos may take hours. Support pause and resume.

**Later:** portable mode (marker file next to the .exe), near-duplicate detection with perceptual hashing (suggest, never hide automatically), Takeout album import, hiding originals behind edited files, an installer, additional formats.

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
| People from Takeout | Names in Takeout sidecars are stored per photo (union across files, never removed by a rescan; no backfill for photos scanned earlier), shown in the info panel, searchable, filterable, and listed in a People sidebar section. Caption, favourite, and upload time are read too. Not face recognition. Extends V1 scope (section 8.9) |
| V1 | Sources, Takeout folders, exact duplicates, grid, viewer, search and filters, albums |
| Takeout zips | V1.5 |
| Architecture | One database writer thread, worker processes for CPU work, commit per batch |
| Photo identity | Content hash (BLAKE2b); albums reference photos |
| Duplicates | Exact hash, hidden not deleted, "Has duplicates" filter |
| Primary file | First non-missing file by source added date, then first seen |
| Missing files | Mark missing, keep rows, detect moves by hash, manual per-source removal |
| Dates | Per-file best date with date_source; local wall-clock for sorting, UTC kept when known |
| Takeout folders | Skip Trash, import Archive, album folders collapse as duplicates |
| Edited files | Separate photos, date from the original's sidecar |
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
| Thumbnail layout (V1) | Thumbnails live at `thumbs/ab/cd/<content_hash>.jpg`, shared by all sources and keyed by content hash (section 6.5); a cache written with another layout is cleared once on startup (tracked by the `thumbs_layout` setting). Removing a source or missing items deletes only the thumbnails of photos that are deleted. (Replaced the interim `thumbs/source-<id>/` layout from section 8.1.) |
| Takeout detection | Folder names only, no file opens: a "Google Photos" folder (or localized name) at or directly under the root, or sidecar names next to at least half of a sample of images |
| Adding a source | Refused if the folder is already a source or is inside the library folder; an overlapping folder is allowed after a warning |
| Relocate check | A sample of 50 known relative paths is checked under the new folder, and the user confirms if few are found |
| Remove source | Thumbnails are deleted first (they regenerate if removal stops halfway), then the rows; photos left with no files go too. Removing album memberships waits for section 8.8 |
| Content hash | BLAKE2b, 16-byte digest (32 hex characters), computed in the worker from the single read of the file |
| Primary file rules | First ok file by source added date, source id, first seen, file id; recomputed on every status or membership change. If all its files are missing, the first missing file stays primary so a "Missing" filter can show the photo |
| Photo date | Best date across all of a photo's files, missing ones included, so unplugging a drive does not change dates |
| Move detection | A new file matches any missing row of the same source with the same hash, oldest first, not only rows that went missing in this scan, because a cancelled scan keeps no state. Moves never cross sources |
| Changed hash | A sole file edited in place re-keys its photo (keeping its id and albums); otherwise the file moves to the photo with the new hash, and a photo left with no files is deleted, its album memberships moving with the file |
| Thumbnail cleanup | Removing a source or missing items deletes only thumbnails of photos being deleted; shared thumbnails stay |
| Schema baseline | No version has been released publicly, so the development migrations 1–5 were folded into one baseline schema at version 5 (same tables, columns and column order). Databases below 5 are replaced, not migrated, and no backup is kept for them. Later changes are numbered migrations from 6 on, with the pre-migration backup |
| Takeout sidecar matching | Case-insensitive; image name plus an optional prefix of `.supplemental-metadata`, then an optional `(n)`; a numbered image `X(n).ext` takes `X.ext(n).json` first; the counter must match; long names match by prefix of at least 30 characters. Ties: title match, exact before truncated, original before edited, then name; greedy, so the result does not depend on order |
| Edited files | Use their own sidecar if any, otherwise share the original's. `-edited` is confirmed; localized suffixes are unconfirmed guesses |
| Takeout location | `geoData` only (ignored when both coordinates are 0); no fallback to `geoDataExif`, so a location removed in Google Photos does not return |
| Takeout Trash | Skipped only directly under the root or a "Google Photos" folder; deeper folders named Trash are imported. Localized names unconfirmed |
| Albums during a scan | Album actions (create, rename, delete, add, remove) are disabled while a scan runs, so the scan thread stays the only database writer |
| Albums | Names unique, case-insensitive, trimmed. `ON DELETE CASCADE` on both album_photos keys covers album, photo, and source deletion. Album counts exclude missing photos; memberships of missing photos are kept |
| Search text | Words split on whitespace and AND-ed, each a case-insensitive `LIKE` on the relative path (filename and folders) of any of its files, with `%`, `_` and `\` escaped |
| Filters | Source, folder and text must hold for one file; folder matches whole path segments; date range inclusive, excluding unknown dates when set; "Missing" shows only photos with no ok file; "Has duplicates" means more than one file row. Filtered queries pin the `taken_at_local` index, because SQLite otherwise sorts every row |
| Grid in the app | `PhotoGrid` with a database data source whose row and count queries are injected (plain, filtered, or album); total height known from month counts before all rows load; unloaded cells paint as placeholders. Month counts use index seeks (about 25 ms at 100,000), so the unfiltered grid uses them instead of the filtered query |
| Scan rows in the grid | Inserted at their sorted positions keeping the selection, current photo and top visible photo; only photos that became visible in the batch are reported, so a new file of a photo never shows twice |
| Date logic (V1) | Pure functions in `imagory/dates.py`; "now" and the computer's time zone are injectable for tests |
| Filename dates | Patterns match at the start of the name, case-insensitively; WhatsApp date-only names give midnight local time; invalid, pre-1990 and future filename dates fall through to the modified time. `VID_YYYYMMDD_HHMMSS` added 2026-11-07 so Android videos in a Takeout cleanup get a filename date |
| Takeout JSON vs EXIF | Less than 24 h apart: EXIF wall-clock as local time plus JSON UTC, `date_source = takeout_json` (pending confirmation, section 18). 24 h or more: JSON wins, converted with the computer's zone. With JSON present, its UTC overrides EXIF `OffsetTimeOriginal` |
| Photo date across files | Best source rank (takeout_json, exif, filename, mtime); ties go to a known UTC time, then the earliest local time, then the earliest UTC time, so input order never matters |
| Grid view (V1) | Custom `QAbstractScrollArea` with a Qt-free layout computed from per-month counts (bisect lookups); data source and thumbnail provider are interfaces, so the widget never touches the database; selection stored as a bytearray; vertical scrollbar always shown to avoid reflow loops |
| Fake Takeout truncation | Sidecar stem cut to 46 characters before `.json` (51 in total), with a numbered duplicate's counter added after the cut; to validate against real exports |
| Takeout in the scan | Sidecars are matched per folder after the walk and read only for new or changed images, so a sidecar added later is picked up only by a source type change (recompute). The matching report covers every image in the walk. Location is stored on the photo and never cleared by a missing one; edited copies inherit the original's date and location |
| Date recompute | "Change type…" in the Sources dialog calls `sources.change_source_type`, which sets the type and recomputes dates in one transaction (no photo re-read, Trash rule not applied retroactively), then reloads the grid and reports how many files changed. A Takeout source needs its folder present; otherwise the user is warned and neither the type nor the dates change. Disabled during scans |
| Filter and album view | The album view is the filter with `album_id` set, so they combine with AND; the filter bar has no album control. Scan-added rows are checked by `photo_matches`, which rejects rows for filters needing the database (missing, duplicates, album) until the end-of-scan reload. A filter or album change resets the scroll; "Remove from album" needs no confirmation; the status bar shows the filtered count only |
| Viewer | Zoom is relative to fit (up to 16x); zooming past fit loads the full-resolution image. The info panel is hidden by default (`I` key or Info button). Show in Explorer acts on the primary file: `explorer /select,"<path>"` on Windows, passed as a command line so only the path is quoted (an argument list quotes the whole switch when the path has a space, and Explorer then opens a default folder, found on Windows 11), `wslpath` plus `explorer.exe` on WSL, otherwise the containing folder opens. The fix is untested on real Windows |
| Takeout cleanup | Brought forward from "Later" (optional export of organized copies), decided with the author 2026-11-07. Writes a deduplicated `YYYY/MM` copy of an extracted Takeout plus `.imagory/` (database, `metadata.json`, reports) to a folder the user picks; originals are never touched, and deleting them is left to the user (section 8.10, `docs/takeout-cleanup.md`). Principle 1 gets an exception for Imagory-created cleanup outputs only |
| Cleanup copies | One copy per content hash; a file already in the output wins, then a year folder, a non-album folder, the shortest path, path order. Every non-sidecar file is copied (videos, RAW, unreadable images as-is; non-sidecar JSON under `.imagory/takeout-json/`). Trash is skipped and reported. Both original and `-edited` copies are kept. Same-stem companions with no sidecar follow their photo |
| Cleanup dates | Sidecar, then EXIF, then filename; never the file modified time (in a Takeout it's the extraction time). Undated files go to `Unknown date/` and stay undated in the output and in a `cleaned` source. Video container dates are not read (no new dependency). Copies' mtime is set to the taken time |
| Cleanup names | Sidecar title with the file's real extension (`.jpg`/`.jpeg` equal), `-edited` suffix kept, on-disk name without a sidecar; sanitized for Windows; casefold clashes get ` (n)`; paths over 250 characters shortened; all renames reported |
| Cleanup safety | `.part` file, source hashed while copying and compared with the analysis, `.part` re-read and hashed, then renamed. "Safe to delete" only when every non-Trash input file is accounted for against the full walk inventory. Free space checked first |
| `cleaned` source | New source type. A `cleaned` scan reads metadata from `<root>/.imagory/imagory.db` by path, confirmed by hash; detected by Add source; no "Change type…". Albums from a run are created once on Add to library, with `<name> (Takeout)` on a clash and existing albums never changed; Takeout album folders and Archive become albums only through cleanup. The old Takeout source can be removed afterwards, with confirmation |
| User-chosen source types | `SOURCE_TYPES` (`plain`, `takeout`) are the types a user can choose; `ALL_SOURCE_TYPES` adds `cleaned`, so a source can't be changed to or from `cleaned` |
| Cleanup provenance | `file_origins` rows always point at the kept row; each input file's own path is in `original_relative_path`, and `album_folder` holds the album's display name (album JSON title, folder name, or `Archive`). Finalise gives kept rows the best non-mtime date of all their duplicates, including those from later runs, and drops failed copies so a rerun plans them again |
| Cleanup resume | Cancel and resume need no saved state beyond the output database. A destination that already exists is never overwritten: identical content counts as done, anything else is a failure. Resume tolerates 1 µs of mtime difference (NTFS has 100 ns steps), removes only the `.part` files of pending copies, and retries failed copies. Renames are stored with the run (output `settings`) for the report |
| Cleanup output database | The main library opens it with `mode=ro&immutable=1`, or reads a temporary copy when a non-empty `-wal` exists; plain `mode=ro` is never used, because it would leave `-wal`/`-shm` files in the user's folder. If it is missing, date recompute for the `cleaned` source refuses and the scan uses the plain rules with a warning, instead of re-dating every file by mtime |
| Cleanup report | Plain UTF-8 text with fixed sections (Counts, Trash skipped, Duplicates, Unmatched sidecars, Unreadable files, Renames, Not accounted for); control characters in names are escaped so one entry stays on one line. `metadata.json` keys follow the export fields in order, with `null` for unknown values |
| Cleanup wizard | Cancel stops the current job without asking; only closing the wizard during work asks first, and the job then keeps cancelling in the background. A job requested while the previous thread is ending waits in a queue that Cancel clears. Closing the app during a copy waits for the current file |
| Fake Takeout cleanup cases | Opt-in (`make_fake_library.py --takeout --cleanup`), so the existing `--takeout` output and manifest don't change; `manifest["cleanup"]` is the ground truth. `--second-account` makes a second export sharing and clashing with the first |
| Linux build | Added for 0.2 at the author's request (2026-11-07), reversing "other platforms not in scope" for Linux x86_64 only. Same PyInstaller spec and code, no Linux-only features; built on Ubuntu 22.04 for the widest glibc reach; Windows stays the main target and the one the section 7.5 checks cover |
| Editable people | Not in V1: people from Takeout are read-only. Planned for V1.x and flagged as needing planning (section 14). Considered and rejected: storing people as albums, because a rescan would undo manual edits (scans touch people but never albums) |
| Grid query thread | The grid's background query runs on a one-thread `QThreadPool` (`GridQuery`), not a new `QThread` per reload: deleting finished `QThread`s intermittently crashed the UI tests ("pure virtual method called"). Root cause in C++ still unknown; details in `docs/archive/issues.md` |
| `db` package (2026-11-09) | `db.py` (2,700 lines) split into `imagory/db/`, one module per area, for readability. `imagory.db` re-exports every public name, so call sites are unchanged and "all SQL in one place" now means the package. Helpers shared between modules keep their leading underscore and are imported explicitly |
| Website (2026-11-12) | Static Astro site in `frontend/` on Vercel (`imagory.mshahnawaz.me`), no Starlight; layout modelled on themia.app with Imagory's colours. Version typed by hand in `src/site.ts`; downloads link to GitHub release files. The **website** uses cookieless Vercel Web Analytics (page views, downloads through `/download/<os>` pages); this does not change section 11, which covers the **app**. The site must not claim "no tracking" |

## 17. Risks

- **Takeout complexity:** inconsistent filenames, sidecar mismatches, and rewritten metadata may cause incorrect dates or missed duplicates. Mitigated by tolerant matching, the matching report, and date_source.
- **Grid with month headers:** a custom virtualized view is the most complex UI part. Prototype it early with the 100,000-photo fake library; fall back to a flat grid if it blocks V1. The prototype meets the target with in-memory items (section 10); keep the fallback until it is confirmed with real thumbnails on Windows.
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
7. **Takeout JSON within 24 h of EXIF:** the date is stored with `date_source = takeout_json` so it keeps the top rank across files; `exif` is the alternative. Confirm.
8. **Future EXIF dates:** the 1990 and future limits apply only to filename dates. Decide whether EXIF dates in the future (wrong camera clock) should be rejected too.
9. **Takeout cleanup on a real export:** check title quirks (wrong extensions, converted HEIC, missing titles) and video sidecar matching against a real Takeout; the cleanup report lists every rename to tune the rules.
