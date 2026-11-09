# Takeout cleanup: design

Make a clean copy of an extracted Google Takeout folder. The copy has one file per unique photo,
no JSON sidecars, and a Year/Month layout, plus a database and a `metadata.json` that keep all the
Takeout metadata. Afterwards the user can delete the original Takeout and lose nothing.

Status: **implemented** (0.2.0). PRD §8.10 is the agreed summary; this file holds the detailed
rules, the pipeline, the data model and the `metadata.json` format. The original plan, its task
list and its resolved questions are in `docs/archive/takeout-cleanup-tasks.md`. The main decisions
came from a Q&A with the author on 2026-11-07.

## Decisions

| Topic | Decision |
| --- | --- |
| Originals | Never modified, moved, renamed or deleted. Opened read-only (`rb`). The user deletes the Takeout by hand if they want to. Tests assert the input is unchanged |
| Duplicates | One copy per unique content hash (BLAKE2b, as in §8.5). Every other copy is recorded as an *origin* of the kept file, not copied |
| Which copy is kept | A file already in the output (from an earlier run) always wins. Otherwise prefer a year folder (`Photos from YYYY`), then a non-album folder, then the shortest relative path, then path order (deterministic) |
| Non-photo files | Videos, RAW and every other non-sidecar file are **copied as-is**, deduplicated by hash, and stored in their own table. Not shown in the grid, only kept safe |
| Unreadable images | An image the scanner can't decode (truncated JPEG, HEIC that pillow-heif rejects) gets no `files` row today. Cleanup treats it as an "other file": hashed, copied as-is, listed in the report |
| JSON files | Only JSON files used as an image's sidecar are left out of the copy; their data goes into the database. Every other JSON file (unused sidecars, album `metadata.json`, account-level files) is copied unchanged to `.imagory/takeout-json/<original relative path>` and listed in the report. If an album folder holds an album metadata JSON with a title, that title names the album instead of the (possibly truncated) folder name. To confirm on a real export |
| Live and motion photo pairs | A companion file with the same stem in the same input folder (for example `IMG_1234.MOV` next to `IMG_1234.HEIC`) and no sidecar of its own follows its photo: same target folder, same name stem |
| Trash | Not copied. Listed in the report, so the user knows what was left out |
| Archive | Copied into Year/Month like everything else and also recorded as an album named `Archive` |
| Album folders | Each Takeout album folder becomes an album in the output database, and in the main library when the output is added. Membership comes from every copy of a photo, including the duplicates that were not copied |
| Edited copies | Both the original and `-edited` are kept (current §8.3 rule). The edited copy takes the original's date |
| Layout | `<output>/YYYY/MM/<name>`, using the photo's best date |
| Dates in a cleanup | The order is sidecar, then EXIF, then filename (§8.4 without the last step). **The file's modified time is never used**, because in Takeout it is the time the zip was extracted. A file with none of the three goes to `<output>/Unknown date/` and is stored with no date (`taken_at_local`, `taken_at_utc` and `date_source` are NULL; `null` in `metadata.json`). In the main library a `cleaned` source keeps these files undated, so they appear in the grid's "Unknown date" section instead of falling back to the copy's mtime. Decided 2026-11-07 |
| Filenames | The sidecar `title` (the original name before Takeout truncated it). If the title's extension differs from the file's (`.jpeg`/`.jpg` count as equal), the title stem is used with the real extension. Edited copies get `<title stem>-edited<ext>`. With no sidecar, the on-disk name is used. Titles are sanitized: `\ / : * ? " < > \|`, control characters, trailing dots and spaces, and reserved names (`CON`, `NUL`, …) are handled |
| Name clashes | Compared with casefold within the target folder, against both the files planned in this run and files already in the output. A clash gets ` (1)`, ` (2)`, … before the extension |
| Long paths | If the full output path would exceed 250 characters, the stem is shortened (extension kept) and the change is logged |
| Video dates | Sidecar date, then filename date (PRD §8.4 patterns, including `VID_YYYYMMDD_HHMMSS`, added 2026-11-07), otherwise `Unknown date/`. Dates inside video files (MP4 `creation_time`) are not read, so no new dependency is needed. Decided 2026-11-07 |
| File times | Each copy's modified time is set to its taken time (local wall clock), so other apps sort it correctly. Files with no real date keep their original mtime. This writes only to Imagory's own copies |
| Copy safety | Each file is written to `<name>.part`. The source is hashed while copying and must match the hash from the analysis (this catches a source that changed). The `.part` is then re-read and hashed, renamed into place, and marked done |
| "Safe to delete" | Checked against the **full walk inventory**, not only the planned copies: every file under the input except Trash must be accounted for, either as a verified `done` copy, as an origin whose hash matches a copied file, as a sidecar whose data is stored, or as a JSON file copied to `takeout-json/`. Any file that isn't accounted for is listed, and the message is not shown |
| Output database | `<output>/.imagory/imagory.db` uses the normal Imagory schema (same `imagory.db` package and migrations) plus thumbnails in `.imagory/thumbs/`. The walk already skips dot folders, so a later scan never indexes it. Its file paths are relative to the output root, and readers find the root from the database's own location (`.imagory/..`), so moving or renaming the output folder keeps working |
| `metadata.json` | `<output>/.imagory/metadata.json` is a plain-text copy of all metadata, readable without Imagory (format below). It is rewritten in full at the end of every run (temp file, then rename) |
| Report | `<output>/.imagory/reports/cleanup-<UTC timestamp>.txt`: counts, skipped Trash, duplicates and where they were, unmatched sidecars, unreadable files, renames for clashes and long paths. The final page links to it |
| Input | One extracted Takeout folder per run. Detection uses `takeout.detect_source_type`. A folder not detected as Takeout gets a warning, and the user may continue |
| Output folder | First run: an empty folder. Later runs may target an existing cleanup output (for example a second Google account). New files are deduplicated against what is already there and merged in. A cancelled run resumes when the same input is picked again |
| Folder checks | Output not inside input, input not inside output, and neither inside the library folder. An output folder that is already a source of another type (`plain`, `takeout`) is refused. A `cleaned` source is allowed (adding a run to it) and is rescanned once the run is added to the library. A warning (as in §6.4) appears if the output is in OneDrive, Dropbox, Google Drive or on a network share, because it holds SQLite |
| Free space | Checked before copying: bytes to copy plus about 5% for thumbnails and the database. Copying is refused if there isn't enough |
| Using the output | After a successful run, the wizard offers **Add to library**: the output becomes a source of type `cleaned` in the main library, and the run's albums are created there. A `cleaned` scan reads dates, people, caption, favourite, upload time and location from the output database (matched by relative path, confirmed by hash), keeping `date_source = takeout_json`. A file not in the output database falls back to the plain-folder rules. **Add source** on a folder containing `.imagory/imagory.db` detects it as `cleaned`. **Change type…** is not offered for `cleaned` sources, and `recompute_dates` for them reads the output database instead of sidecars |
| Old source swap | If the input is already a source in the main library, the wizard offers (with confirmation) to remove that source **after** the cleaned source has been added and scanned. Photos keep their ids and albums because identity is the content hash. Only database rows are removed; files are untouched |
| Albums in the main library | Created once, when a run's output is added (scans never touch albums, as in §16). If an album with the same name (case-insensitive) already exists, a new album `<name> (Takeout)` is created instead, then `<name> (Takeout 2)` and so on. Existing albums are never changed (decided 2026-11-07) |
| Concurrency | A `CleanupThread` (QThread) is the only writer of the output database. Worker processes hash, read metadata and make thumbnails, as in §6.2. The main library database is not touched until **Add to library**, which uses the normal `ScanThread`. Cleanup is disabled during a scan, and scanning and source actions are disabled during a cleanup |
| Edits after cleanup | The output database is a record of the Takeout metadata. Album or people edits made later in the main library are not written back to it |
| Implementation details | Decided during the agent waves (2026-11-07) and recorded in PRD §16: Migration 5, Cleanup provenance, Cleanup resume, Cleanup output database, Cleanup report, Cleanup wizard, Fake Takeout cleanup cases. PRD §8.4 now limits the mtime ban to dates that come from a Takeout |

## Pipeline

All stages can be cancelled and resumed without extra saved state, in the spirit of §6.3. Each stage
picks up from what is already committed in the output database.

1. **Check.** Validate the folders (rules above), open or create `<output>/.imagory/imagory.db`,
   create or resume a `cleanup_runs` row for this input.
2. **Analyse photos.** Add the input as a temporary `takeout` source in the output database and run
   the existing `Scanner` on it unchanged: walk, sidecar matching, hash, metadata, dates, people,
   location, thumbnails. Trash is skipped by the existing rule.
3. **Analyse other files.** Walk the input again with all extensions (same walk rules), keeping the
   full inventory for the "safe to delete" check. Every file that is neither a stored image nor a
   used sidecar goes into `extra_files`: non-images, images the scanner couldn't read, and
   JSON files that aren't sidecars. They are hashed in the worker pool and their sidecars are matched
   (`takeout.match_directory`), so each row carries its sidecar date and people when there is one.
4. **Plan.** Choose the kept copy per hash, compute the target folder and name, resolve clashes, and
   record every copy (kept or not) in `file_origins`, with its Takeout album folder. Write the
   `cleanup_copies` rows (`pending`).
5. **Preview** (wizard page, nothing copied yet): unique photos, other files, duplicates skipped
   (count and bytes saved), Trash files skipped, unmatched sidecars, albums found, bytes to copy and
   free space.
6. **Copy.** Work through the `pending` rows in a small thread pool (I/O bound): `.part` file,
   hash checks, rename, set mtime, mark `done`. On resume, a `done` row whose file still matches
   its size and mtime is skipped. A leftover `.part` is overwritten.
7. **Finalise** (one transaction): move the run's file rows from the temporary input source to the
   output's `cleaned` source with their new paths **and the copies' own size and modified time**
   (mtime was changed, so the input's values would make every file look changed), move
   `extra_files` likewise, create albums
   (album folders and `Archive`), mark the run finished, and delete the temporary input source's
   rows. Then write `metadata.json` and the report.
8. **Add to library** (optional, from the final page): add or rescan the `cleaned` source in the
   main library, create the run's albums, then offer the old-source swap.

## Data model

Added to every database, so there is one schema. Used only in output databases, apart from the new
source type.

```
sources        type CHECK adds 'cleaned'               -- needs a table rebuild, see Risks
cleanup_runs   id, input_path, input_label, started_at, finished_at,
               status ('analysing' | 'planned' | 'copying' | 'done' | 'cancelled'),
               photos, other_files, duplicates, trash_skipped, errors, bytes_copied
file_origins   id, run_id, file_id NULL, extra_file_id NULL,   -- exactly one is set
               original_relative_path, sidecar_relative_path, album_folder NULL
extra_files    id, source_id, relative_path, size, modified_time, content_hash, extension,
               taken_at_local, taken_at_utc, date_source, latitude, longitude,
               description, favorite, uploaded_at_utc, people (JSON text)
               UNIQUE (source_id, relative_path)
cleanup_copies id, run_id, file_id NULL, extra_file_id NULL, source_relative_path,
               target_relative_path UNIQUE COLLATE NOCASE, status ('pending' | 'done' | 'error'),
               error NULL
```

Indexes: `file_origins(file_id)`, `file_origins(extra_file_id)`, `extra_files(content_hash)`,
`cleanup_copies(run_id, status)`. Paths follow §9 (relative, `/`, on-disk case, `COLLATE NOCASE`).

## `metadata.json` format

```json
{
  "format": "imagory-takeout-cleanup",
  "version": 1,
  "generated_at": "2026-11-07T10:00:00Z",
  "runs": [{"id": 1, "input_path": "D:/Takeout", "finished_at": "…Z", "counts": {}}],
  "albums": [{"name": "Trip 2019", "files": ["2019/07/IMG_1234.jpg"]}],
  "files": [{
    "path": "2019/07/IMG_1234.jpg", "kind": "photo", "size": 123456,
    "content_hash": "…32 hex…", "width": 4032, "height": 3024, "format": "JPEG",
    "taken_at_local": "2019-07-04T12:00:00", "taken_at_utc": "2019-07-04T10:00:00Z",
    "date_source": "takeout_json", "latitude": 48.85, "longitude": 2.35,
    "people": ["Ana"], "description": "", "favorite": false, "uploaded_at_utc": "…Z",
    "origins": [{"run": 1, "path": "Takeout/Google Photos/Photos from 2019/IMG_1234.jpg",
                 "sidecar": "…/IMG_1234.jpg.supplemental-metadata.json", "album": null}]
  }]
}
```

`kind` is `photo` or `other`. Fields that don't apply to a kind are omitted. Names come from
sidecars and are untrusted text, so the JSON is written with escaping, never built by hand.

## Modules

- `imagory/cleanup/`: the feature's own package. `types` (shared dataclasses and errors),
  `checks` (input and output folder checks), `inventory` (every input file, sorted into photos,
  other files and JSON), `planning` (pure rules: kept-copy choice, target folder, name from title,
  sanitizing, clash resolution, long-path shortening, space estimate), `hashing` (hash workers),
  `copying` (the copy-and-verify step), `export` (`metadata.json` and the report), and `pipeline`
  (`CleanupPipeline`, the stages in order). No Qt.
- `imagory/db/cleanup.py` and `imagory/db/cleaned.py`: all cleanup SQL (runs, origins, extra
  files, copies, finalise, `metadata.json` export rows) and reading an output database for a
  `cleaned` source.
- `imagory/scanner.py`: the `cleaned` source type reads per-file metadata from the output database
  instead of sidecars.
- `imagory/sources.py`: adding a `cleaned` source, importing a run's albums, and the swap.
- `imagory/paths.py`: `cleanup_dir(output)` → `<output>/.imagory`, plus the db, thumbs and reports paths.
- `imagory/ui/cleanup_wizard.py` and `imagory/ui/cleanup_thread.py`.
- `imagory/ui/sources_dialog.py`: shows the `cleaned` type, hides "Change type…" for it, and Add
  source detects a cleanup output.
- `imagory/ui/main_window/cleanup.py`: the **Clean up Takeout…** action's controller (wizard, jobs,
  Add to library, swap); `window.py` enables and disables actions around scans and cleanups.
- `scripts/make_fake_library.py --takeout`: videos with sidecars, an album folder with duplicate
  files, an Archive folder, a truncated name with a full `title`, a title containing `:` and `/`,
  two titles that differ only in case, a corrupt JPEG, a motion-photo pair (HEIC plus a MOV with no
  sidecar), an unused sidecar, and an album metadata JSON.
## UI

The entry point is a toolbar ("header") button **Clean up Takeout…** next to Add source, plus the
same action in the File menu. It opens a non-modal wizard window, so the grid stays usable. Closing
the window while work is running asks whether to cancel.

1. **Input:** choose the Takeout folder. Shows the detected type, or a warning if it isn't
   recognised as Takeout.
2. **Output:** choose a folder. Shows "empty: new cleanup", "existing cleanup: N files, will add to
   it", "resume cancelled run", or the reason it is refused.
3. **Analyse:** progress (files processed out of found) and Cancel.
4. **Preview:** the counts from pipeline step 5, free space, and **Start copying**.
5. **Copy:** progress (files and bytes) and Cancel.
6. **Done:** a summary, a verification result ("All N files copied and verified. You can delete the
   original Takeout folder if you want; Imagory never does."), or the list of failures. Buttons:
   Open folder, Open report, **Add to library** (then the swap offer when it applies).

All text shown from sidecars is escaped, as in the info panel.

## Risks

- **Disk space and time:** a cleanup reads the input about twice (analysis, then copy with source
  hash), writes it once and re-reads it once, then the main library scans the output once more.
  Reusing the output's hashes and thumbnails in the main library scan (same `thumbs/ab/cd/<hash>.jpg`
  layout) is a possible later optimisation.
- **Title quirks** in real exports (wrong extensions, converted HEIC, missing titles) need checking
  against a real Takeout. The report lists every rename so the rules can be tuned.
- **Sidecar matching for videos** has not been checked on real exports yet.
- **Windows:** long paths, reserved names, and mtime setting on NTFS must be checked on real Windows,
  not WSL.

## Out of scope

Deleting or moving originals (always manual), Takeout zips (V1.5), writing EXIF or XMP into copies,
near-duplicate detection, opening the output as its own library or switching libraries, rebuilding
a database from `metadata.json`, and syncing later edits back into the output database.
