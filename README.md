<p align="center"><img src="assets/imagory-logo.svg" alt="imagory" height="64"></p>

# Imagory

A free, open source photo browser for Windows. Point it at your photo folders and
extracted Google Takeout exports and get one fast, browsable library.

> **Your photos stay yours.** Imagory is fully offline. It makes no network connections:
> no telemetry, no accounts, no update checks. It only *reads* your photos. It never
> changes, moves, renames, or deletes them, and never writes anything into your photo
> folders. Everything it stores (database, thumbnails, logs) stays on your computer in
> its own library folder. The one exception is **Clean up Takeout**, which writes copies
> only into an output folder you pick (an empty one, or an earlier cleanup output); the
> original export is still only read.

**Status: V1 in progress.** Add several photo folders as sources, scan them in the
background, browse all their thumbnails in one grid, view photos, and reopen the library
later without rescanning. Exact duplicates show once, moved files are recognised, and
dates come from EXIF (with its time zone when known) or the filename. Takeout dates,
locations, and the people tagged in Google Photos are read from the JSON files. The sidebar
has a People section, and you can search and filter by person (names are only copied from
Takeout, Imagory does no face recognition). **Clean up Takeout** makes a
deduplicated, sorted copy of an export without touching the original. Photos scanned before this feature get names
once their source is removed and added again. See [PRD.md](PRD.md).

[![Architecture diagram](https://gitdiagram.com/diagram-badge.svg)](https://gitdiagram.com/mshahnawaz1/imagory?utm_source=readme&utm_medium=badge)

## Using Imagory

1. Start Imagory and click **Add a folder…** (or **Add source…** later). Give the source a
   label, such as "Phone backup". Imagory detects whether the folder is a Google Takeout
   export and shows why; you can change the type. It then scans the folder and its
   subfolders in the background, and photos appear in the grid as they are processed. You
   can cancel at any time, and **Rescan all** picks up where it stopped.
2. Double-click a thumbnail (or press Enter) to open the viewer. **Left** and **Right** move
   between photos. **Back to photos** (or Esc, Backspace, or the mouse back button) returns
   to the grid.
3. **Rescan all** finds new, changed, and missing photos in every source. Unchanged photos
   are skipped, so it is quick. Missing photos are hidden, not forgotten. They come back if
   the file returns. A file you moved to another folder in the same source keeps its place
   instead of showing up as missing and new. A source whose folder is not available (for
   example an unplugged drive) is reported, and the other sources are still scanned.
4. **Sources…** lists your sources with their photo and missing counts. From there you can
   add, rescan, rename, or change the type of a source. **Relocate…** points a source at
   its new folder when a drive letter changes or the folder moved, without rescanning
   from scratch. **Remove missing…** makes Imagory forget the source's missing files, and
   **Remove…** forgets the whole source and its thumbnails, both after you confirm. The
   photos themselves are never touched.
5. **Duplicates:** identical files (same contents, anywhere in your sources) appear once in
   the grid. Select photos with click, Ctrl-click, Shift-click, or Ctrl+A; the status bar
   shows how many are selected.
6. **Clean up Takeout…** (toolbar or File menu) makes a tidy copy of an extracted Google
   Takeout export. See below.

### Cleaning up a Google Takeout export

A Takeout export is messy: a JSON file next to every photo, the same photo in several
album folders, truncated file names, and file dates that are just the time you unzipped
it. **Clean up Takeout…** copies it into a new folder you choose:

- one copy of each photo or video (exact duplicates are copied once), in `YYYY/MM`
  folders by the date the photo was taken, or `Unknown date` when there is no real date;
- the original file names from Google Photos, made safe for Windows;
- no JSON files next to the photos. Their dates, places, people, captions and favourites
  are kept in `.imagory\imagory.db` and in a plain-text `.imagory\metadata.json`, and the
  albums are kept too. A report lists everything skipped, renamed or not copied.

The wizard analyses the export first and shows what it will copy, how many duplicates it
found and how much disk space it needs (about the size of the photos plus 5%). Nothing is
copied until you click **Start copying**. Every copy is checked against the original
before it counts as done. You can cancel at any time, and picking the same folders again
carries on where it stopped. A second export (for example another Google account) can be
added to the same output folder.

**Your original export is never changed, moved or deleted.** When the wizard says it is
safe to delete, every file in the export (except Trash) is accounted for in the copy, and
you can delete the export yourself if you want to. Imagory never does it for you.

**Add to library** adds the output folder as a source of type **cleaned** and creates its
albums (an album whose name is already taken gets `(Takeout)` added). Photos in a cleaned
source keep the dates and people from the export, and files with no real date stay
undated instead of getting the unzip time. If the original export was already a source,
Imagory then offers to remove it, after you confirm; photos keep their albums and people.
Adding a cleanup output with **Add source…** works too. **Change type…** is not offered
for a cleaned source, because its dates come from the cleanup, not from the folder.

Like the rest of Imagory, the cleanup is fully offline.

Supported formats: JPEG, PNG, GIF (first frame), and HEIC/HEIF. Photos are sorted newest
first by the date the photo was taken (EXIF), then a date in the filename (such as
`IMG_20190131_235959`), then the file's modified time, with month headers.

### Where Imagory keeps its data

The library folder is `%LOCALAPPDATA%\Imagory\` and contains `imagory.db` (the database),
`thumbs\` (a thumbnail cache you can delete at any time), and `logs\`. **Help > Open log
folder** opens the logs, for example to attach them to a GitHub issue. Logs are never sent
anywhere.

A library created by the early V0 test build is not carried over: Imagory starts with an
empty library and you add your folders again. Your photos are not affected.

### Windows SmartScreen and antivirus warnings

Imagory releases are not code-signed, so Windows SmartScreen may say "Windows protected
your PC". Click **More info**, then **Run anyway**. Some antivirus tools also flag unsigned
single-file programs built with PyInstaller. If you'd rather not trust the download, you can
build Imagory yourself from source (below).

## Development

Requires [uv](https://docs.astral.sh/uv/). Development happens on Linux (WSL) or Windows;
the target is Windows 10/11.

```sh
uv sync                                    # install dependencies into .venv
IMAGORY_LIBRARY=.dev-library uv run python -m imagory   # run with a scratch library
uv run pytest -q                           # tests
uv run ruff check . && uv run ruff format --check .
```

Always set `IMAGORY_LIBRARY` to a scratch folder in development, so your real library is
never touched. Tests set it automatically.

### Performance testing

```sh
uv run python scripts/make_fake_library.py .fake-library --count 100000
IMAGORY_LIBRARY=.bench-library uv run python scripts/bench.py .fake-library
```

Results are recorded in [docs/v0-results.md](docs/v0-results.md).

A synthetic Google Takeout export, with JSON sidecars using Takeout's naming quirks and a
ground-truth `manifest.json`:

```sh
uv run python scripts/make_fake_library.py .fake-library-takeout --takeout --count 2000
```

Time a Takeout cleanup of a generated export (the output goes into the scratch folder):

```sh
uv run python scripts/bench_cleanup.py /path/to/scratch --count 50000
```

The V1 grid prototype (100,000 in-memory items with month headers):

```sh
uv run python scripts/grid_prototype.py          # interactive window
uv run python scripts/grid_prototype.py --bench  # print timings
```

### Building the Windows executable

Run on Windows (not WSL):

```sh
uv sync
uv run python scripts/collect_licenses.py   # refresh THIRD_PARTY_LICENSES.txt
uv run pyinstaller --noconfirm --clean imagory.spec
```

The result is `dist\Imagory.exe`, a single file.

## Licence

Imagory is released under the [MIT licence](LICENSE). Bundled third-party components keep
their own licences, listed in [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt).
PySide6/Qt is used under the LGPL v3. Note: the pillow-heif binary wheels (HEIC support)
bundle the GPLv2 x265 library. This is an open question to resolve before the first public
release (PRD §18).
