# Takeout cleanup: agent todo

Work breakdown for `docs/takeout-cleanup-plan.md` (the plan holds the decisions; this file holds
who does what, in which order, on which model). Agents run in parallel, each on its own branch in
its own git worktree. A lead session on a larger model plans, monitors, reviews, and merges.

Tick a box only when the item is implemented, tested, and committed on its branch. The lead ticks
the **Merged** column in the status table.

> `agents_docs/` is git-ignored, so worktrees don't contain this file. Agents read it by its
> (archived; it was `agents_docs/takeout-cleanup-todo.md` while the waves ran).
> `docs/takeout-cleanup-plan.md` and the wave 0 contracts must be **committed on `main` before
> wave 1 starts**, so every worktree has them. Ask the author before committing.

## Roles and models

| Role | Model | Use it for |
| --- | --- | --- |
| **Lead / monitor** | Opus 5.5 (`claude-opus-5-5`), the main session | Wave 0, writing the contracts, spawning agents, reviewing every branch, deciding questions agents raise, merging, PRD/README/tasks updates. Never hands its review to a smaller model |
| **Opus agents** | Opus 5.5 | Cross-cutting or data-safety work: the migration that touches every existing library, the pipeline orchestrator, scanner changes |
| **Sonnet agents** | Sonnet 5.5 (`claude-sonnet-5-5`) | Well-defined modules with a clear contract: pure planning logic, the copy primitive, Qt widgets, the fake data generator, wiring |
| **Haiku agents** | Haiku 5.5 (`claude-haiku-5-5`) | Small mechanical tasks with an exact spec: writers for a fixed format, path helpers, a shared test helper, running a benchmark and recording numbers |

Spawn with the Agent tool: `isolation: "worktree"`, `model` set to `"opus"`, `"sonnet"` or `"haiku"` from the task table, and a prompt
that names the task id, the branch, this file's absolute path, and the plan.

### Monitoring rules for the lead

1. **Before spawning:** wave 0 is merged on `main`, and the contracts in this file match
   `imagory/cleanup_types.py`.
2. **While running:** don't poll. When an agent finishes, read its final report, then check the branch:
   `git log main..<branch>`, `git diff main...<branch> --stat`, and confirm no files outside its
   ownership changed (or that each change is listed in the report).
3. **Review each branch** before merging: run `uv run pytest -q`, `uv run ruff check .` and
   `uv run ruff format --check .` in its worktree, then `/code-review` on the branch. Read Haiku
   branches in full; they get no lighter review.
4. **Safety grep** on every branch (PRD principle 1). Any hit on an input path is a blocker:
   `grep -rnE "os\.(remove|unlink|rename|replace)|shutil\.(move|rmtree)|\.unlink\(|\.rename\(|open\([^)]*['\"][wa]" imagory/`.
   Hits are allowed only on paths under the output folder or `.imagory/`, and the code must show that.
5. **Escalate:** if an agent reports being blocked, fails its tests twice, or goes outside its
   files, the lead decides. That means answering the question and resuming the agent with
   SendMessage, taking the task over, or re-running it on the next larger model (Haiku, then
   Sonnet, then Opus). A smaller model is never asked to "try harder" on a design question. The
   lead answers it.
6. **Decisions** that agents report go into the plan's Decisions table, and later into PRD §16.
   Agents never edit `PRD.md`, `README.md`, `CLAUDE.md`, `docs/tasks.md`, the plan, or this
   file.

## Waves at a glance

| Wave | Task | Model | Branch | Needs |
| --- | --- | --- | --- | --- |
| 0 | L0 PRD and docs | Lead | `main` | — |
| 0 | L1 contracts module | Lead | `main` | — |
| 0 | W0 path helpers and test helper | Haiku | `feat/cleanup-w0-helpers` | — |
| 1 | A schema and SQL (migration 5) | **Opus** | `feat/cleanup-a-schema` | wave 0 |
| 1 | B planning functions | Sonnet | `feat/cleanup-b-planning` | wave 0 |
| 1 | C copy-and-verify primitive | Sonnet | `feat/cleanup-c-copy` | wave 0 |
| 1 | D `metadata.json` and report writers | Haiku | `feat/cleanup-d-export` | wave 0 |
| 1 | E fake Takeout additions | Sonnet | `feat/cleanup-e-fixtures` | wave 0 |
| 1 | F wizard UI shell | Sonnet | `feat/cleanup-f-wizard` | wave 0 |
| 2 | G pipeline orchestrator | **Opus** | `feat/cleanup-g-pipeline` | A, B, C, D, E |
| 2 | H `cleaned` source in the main library | **Opus** | `feat/cleanup-h-cleaned-source` | A, E |
| 3 | I thread and main window wiring | Sonnet | `feat/cleanup-i-wiring` | F, G, H |
| 3 | J cleanup benchmark | Haiku | `feat/cleanup-j-bench` | G |
| 4 | L2 final review, docs, Windows handoff | Lead | `main` | all |

Wave 1 has six agents running at once. Run fewer at a time if machine resources or token budget
are tight; the merge order below stays the same.

## Wave 0 (lead, on `main`)

### L0 PRD and docs (lead)
- [x] `PRD.md`: add §8.10 Takeout cleanup, and edit §3 (principle 1 exception, non-goals), §8.4 (no mtime fallback for cleanup and `cleaned`), §9 (migration 5), §14 (export brought forward) and §16 (one row per plan decision)
- [x] `docs/tasks.md`: add "Takeout cleanup" under upcoming work, linking the plan and this file
- [x] Commit the plan and the PRD changes (`docs(prd): plan Takeout cleanup`), after the author agrees

### L1 contracts module (lead)
- [x] Write `imagory/cleanup_types.py` with exactly the dataclasses in **Contracts** below (frozen, type-hinted, no logic), plus a test that imports it
- [x] Commit it (`feat(cleanup): add shared cleanup types`)

### W0 helpers (Haiku)
- [x] `imagory/paths.py`: `cleanup_dir(output) -> <output>/.imagory`, `cleanup_db_path`, `cleanup_thumbs_dir`, `cleanup_reports_dir`, `cleanup_json_dir` (`takeout-json`), `metadata_json_path`. Path math only, no folder creation. Tests in `tests/test_paths.py`
- [x] `tests/fs_snapshot.py`: one `snapshot(root) -> dict[str, tuple[int, int]]` (relative path to size and `st_mtime_ns`, all files, dot folders included) and `assert_unchanged(root, before)`. Don't touch the existing copies in other test files. (Lead change: folders are recorded too, as `dir/` keys with `(0, mtime_ns)`, so a new empty folder or a created-then-deleted file in the input fails the check)
- [x] Commit, report. The lead merges W0 before wave 1

## Contracts

All of these live in `imagory/cleanup_types.py`, written by the lead in L1. Only the lead changes
them; an agent that needs a change asks in its report. Paths are `/`-separated relative strings,
following PRD §9.

```python
FileKind = Literal["photo", "other", "json"]  # json = a JSON file that isn't a used sidecar


@dataclass(frozen=True)
class Candidate:  # one file found in the input, or already in the output
    key: int  # caller's id (file / extra_file row), opaque to the planner
    kind: FileKind
    relative_path: str  # input-relative; output-relative when in_output
    content_hash: str
    size: int
    title: str | None  # sidecar title, if any
    has_sidecar: bool
    taken_at_local: str | None  # already chosen: sidecar, EXIF, filename; never mtime
    album_folder: str | None  # Takeout album folder name, "Archive", or None (year folder)
    in_output: bool = False  # copied by an earlier run


@dataclass(frozen=True)
class Rename:
    original: str  # name it would have had
    target: str  # output-relative path it got
    reason: Literal["sanitized", "clash", "long_path", "title_extension"]


@dataclass(frozen=True)
class PlannedCopy:
    key: int
    content_hash: str
    target_relative_path: str  # output-relative; JSON goes under .imagory/takeout-json/


@dataclass(frozen=True)
class Plan:
    copies: tuple[PlannedCopy, ...]  # one per new hash; JSON files are not deduplicated
    kept_key_by_hash: Mapping[str, int]  # includes in-output files
    renames: tuple[Rename, ...]


@dataclass(frozen=True)
class CopyResult:
    ok: bool
    size: int | None
    mtime_ns: int | None
    error: str | None


@dataclass(frozen=True)
class ExportOrigin:
    run: int
    path: str
    sidecar: str | None
    album: str | None


@dataclass(frozen=True)
class ExportFile:
    path: str
    kind: Literal["photo", "other"]
    size: int
    content_hash: str
    width: int | None = None
    height: int | None = None
    format: str | None = None
    taken_at_local: str | None = None
    taken_at_utc: str | None = None
    date_source: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    people: tuple[str, ...] = ()
    description: str | None = None
    favorite: bool = False
    uploaded_at_utc: str | None = None
    origins: tuple[ExportOrigin, ...] = ()


@dataclass(frozen=True)
class ExportRun:
    id: int
    input_path: str
    started_at: str
    finished_at: str | None
    counts: Mapping[str, int]


@dataclass(frozen=True)
class ExportAlbum:
    name: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class CleanupReport:
    counts: Mapping[str, int]
    trash_skipped: tuple[str, ...]
    duplicates: tuple[tuple[str, str], ...]  # (kept output path, skipped input path)
    unmatched_sidecars: tuple[str, ...]
    unreadable: tuple[str, ...]
    renames: tuple[Rename, ...]
    unaccounted: tuple[str, ...]  # non-empty means "safe to delete" is not shown


@dataclass(frozen=True)
class FolderCheck:
    mode: Literal["new", "add", "resume", "refused"]
    message: str


@dataclass(frozen=True)
class PreviewCounts:
    photos: int
    other_files: int
    json_files: int
    duplicates: int
    duplicate_bytes: int
    trash_skipped: int
    unmatched_sidecars: int
    albums: int
    bytes_to_copy: int
    free_bytes: int


@dataclass(frozen=True)
class DoneResult:
    copied: int
    failed: tuple[str, ...]
    unaccounted: tuple[str, ...]
    safe_to_delete: bool
    report_path: Path
    output_path: Path
    input_is_source: bool  # the input is already a source in the main library
```

### Expected names (lead-owned, shared by B and G)

The single source of truth for odd names. B's tests and G's fixture tests use these exact results;
E doesn't compute names. The lead adds rows when a new case comes up.

| Input (title or on-disk name) | Situation | Output name |
| --- | --- | --- |
| `IMG_1234.jpg` | plain | `IMG_1234.jpg` |
| `IMG_1234.jpg` twice in one month folder, different hashes | clash | `IMG_1234.jpg`, `IMG_1234 (1).jpg` (order by kept-copy order) |
| `img_1234.JPG` and `IMG_1234.jpg` | case-only clash (the first in kept-copy ranking keeps the plain name; B's test puts `img_1234.JPG` in the year folder) | `img_1234.JPG`, `IMG_1234 (1).jpg` |
| `CON.jpg` | reserved name | `CON_.jpg` |
| `a/b:c.jpg` | invalid characters | `a_b_c.jpg` |
| `trip. .jpg` | trailing dot and space in the stem | `trip.jpg` |
| `...` or empty title | nothing usable | on-disk name, then `unnamed` + extension |
| title `IMG_1.HEIC`, file is `.jpg` | title extension differs | `IMG_1.jpg` |
| title `IMG_1.jpeg`, file is `.jpg` | equal extensions | `IMG_1.jpeg` |
| `IMG_1234-edited.jpg` (original title `IMG_1234.jpg`) | edited copy | `IMG_1234-edited.jpg` |
| `IMG_1.HEIC` + `IMG_1.MOV` (no sidecar), photo clashed | companion | `IMG_1 (1).HEIC`, `IMG_1 (1).MOV` |
| 300-character title | long path | stem shortened so the full path is ≤ 250 characters, extension kept |

## File ownership

An agent edits only its own files plus new files it creates. Everything else is read only. A
change that is truly needed elsewhere must be kept minimal and listed in the report.

| File | Owner |
| --- | --- |
| `imagory/cleanup_types.py` | lead (L1) |
| `imagory/paths.py`, `tests/fs_snapshot.py` | W0 |
| `imagory/db.py`: migration 5 and new section `# --- cleanup ---` (before `# --- settings ---`) | A; G may add to that section in wave 2 |
| `imagory/db.py`: new section `# --- cleaned sources ---` | H |
| `imagory/cleanup.py` (new) | B |
| `imagory/cleanup_copy.py` (new) | C |
| `imagory/cleanup_export.py` (new) | D |
| `scripts/make_fake_library.py`, its tests | E |
| `imagory/ui/cleanup_wizard.py` (new) | F |
| `imagory/cleanup_run.py` (new) | G |
| `imagory/scanner.py`, `imagory/sources.py`, `imagory/ui/sources_dialog.py` | H |
| `imagory/ui/cleanup_thread.py` (new), `imagory/ui/main_window.py` | I |
| `scripts/bench_cleanup.py` (new) | J |
| `PRD.md`, `README.md`, `CLAUDE.md`, `agents_docs/*`, `docs/takeout-cleanup-plan.md` | lead |
| Everything else (`takeout.py`, `dates.py`, `thumbs.py`, `metadata.py`, other UI) | read only for all |

Each agent puts its tests in new files named after its task: `tests/test_cleanup_schema.py` (A),
`tests/test_cleanup_planning.py` (B), `tests/test_cleanup_copy.py` (C),
`tests/test_cleanup_export.py` (D), `tests/test_cleanup_wizard.py` (F),
`tests/test_cleanup_run.py` (G), `tests/test_cleaned_source.py` (H),
`tests/test_cleanup_wiring.py` (I).

**Migration number:** 5 is A's, and A's only. No other agent adds a migration. Anything else needing
a schema change goes to the lead.

## Lead decisions (wave 1)

Answers given before wave 1 started. They go into the plan's Decisions table at L2.

- **`metadata.json` fields (D):** keys appear in `ExportFile` field order. For `kind: "photo"` every
  field is written, `None` as `null`. For `kind: "other"`, `width`, `height` and `format` are
  omitted; every other field is written, `None` as `null` (so an undated file shows
  `"taken_at_local": null` for both kinds). `people` and `origins` are JSON lists, never omitted.
  Each origin is `{"run", "path", "sidecar", "album"}`. Each run is
  `{"id", "input_path", "started_at", "finished_at", "counts"}`. Albums are `{"name", "files"}`, in
  the order given. `generated_at` is passed in as a string and written as is.
- **Report sections (D):** headings in this order: `Counts`, `Trash skipped`, `Duplicates`
  (`<kept output path>  <-  <skipped input path>`), `Unmatched sidecars`, `Unreadable files`,
  `Renames` (`<original>  ->  <target>  (<reason>)`), `Not accounted for`. An empty list prints
  `(none)` under its heading.
- **Export queries (A):** return the `cleanup_types` dataclasses (`ExportFile`, `ExportAlbum`,
  `ExportRun`) directly; `db.py` may import `imagory.cleanup_types`.
- **Branching:** every agent branches from local `main` (rule 2 below).

### Decisions from wave 1 reports (lead accepted)

- **C:** `cancel` is `Callable[[], bool]`, polled per chunk in both passes. `already_done` allows
  1 µs mtime tolerance (NTFS 100 ns steps). The `.part` is fsynced and gets its mtime before
  `os.replace`. Lead added: **an existing `dest` is never overwritten**; identical content is
  accepted as done (its own size and mtime returned), anything else returns
  `ok=False, "destination exists with different content"`. `remove_stale_parts` returns a count.
- **D:** report lines escape control characters (`\n`, `\t`, `\xNN`) so one entry stays on one
  line; metadata.json is untouched by that. Empty `Counts` prints `(none)`. No title line.
  `render_report(report) -> str` is public. A lone surrogate in a name raises
  `UnicodeEncodeError` (the `.tmp` is removed): **G must catch it** and still finish the run.
- **F:** the Cancel button emits `cancel_requested` without a prompt; only closing the window
  prompts, and after "yes" the window closes while the pipeline keeps cancelling. Extra slot
  `show_cancelled()` returns to the output page. Injectable `pick_folder` and `confirm_cancel`.
- **A:** 12-step rebuild kept (the CHECK still enforces types); `foreign_key_check` runs before the
  commit, so a violation leaves version 4 and the backup. `SOURCE_TYPES` stays "types a user can
  choose" (`plain`, `takeout`); new `ALL_SOURCE_TYPES` adds `cleaned` and is what `create_source`
  accepts, so `set_source_type`/`change_source_type` refuse `cleaned`. H creates cleaned sources
  with `create_source(..., "cleaned")` and adds `TYPE_NAMES["cleaned"]` for the dialog. Copies'
  size and mtime are passed into `finalise_run(conn, run_id, temp_source_id, cleaned_source_id,
  copied: {copy_id: (size, mtime_ns)}, counts)`, not stored. Finalise gives kept rows the best
  non-mtime date of their duplicates, drops `error` copies with their origins (a rerun replans
  them), and deletes the temporary source. Origins/copies have no ON DELETE, so losing
  provenance fails the transaction. `export_albums` reads origins (videos appear as members);
  `export_files` leaves out `.imagory/` JSON copies; only `done` runs are exported. Insert
  functions open no transaction: G wraps `insert_origins` + `insert_copies` +
  `set_run_status("planned")` in one `db.transaction`.
- **Album title (lead):** no `album_title` column. `file_origins.album_folder` holds the album's
  **display name**: the album JSON title when present, else the folder name, or `Archive`. The
  folder itself stays recoverable from `original_relative_path`.
- **B:** clashes are resolved in `choose_kept` ranking order; a photo and its companions claim one
  ` (n)` together; JSON paths go through the same clash and long-path rules; the 250-character
  budget reserves room for ` (n)`; only file stems are shortened, never folders. Public API:
  `choose_kept`, `target_folder`, `sanitize_name`, `name_for`, `shorten_for_max_path`,
  `resolve_clashes(wanted, taken=(), output_root_len=0)`, `build_plan`, `estimate_space`,
  `taken_mtime_ns`; constants `UNKNOWN_DATE_FOLDER`, `JSON_FOLDER`, `MAX_PATH_CHARS`, `UNNAMED`.
- **For G (from B):** (1) candidate `key`s must be **unique across photos, other files and JSON**
  (e.g. extra-file ids negated), because `PlannedCopy` carries only `key`. (2) Every copy of a
  hash gets the **same best date** (PRD §8.4 best date across copies, mtime excluded), so the
  kept copy is never undated while a duplicate has a date. (3) `existing_output_paths` must include
  earlier runs' `.imagory/takeout-json/...` paths.
- **Windows handoff (from B):** the 250 limit counts Python characters, not UTF-16 units (emoji
  count twice on Windows); `CONIN$`/`CONOUT$` and superscript `COM¹` are not in the reserved list.
- **E:** the cleanup cases are **opt-in**: `make_fake_library.py OUT --takeout --cleanup` (Python:
  `make_takeout(..., cleanup=True)`), because an existing test requires every plain `--takeout`
  file to be in `manifest["images"]`. Without the flag the output is unchanged. The new files are
  not in `manifest["images"]`; `manifest["cleanup"]` (`version`, `source_root`, `files`, `groups`,
  `albums`) is the single ground truth. Second account: `make_second_account(out, first_out,
  seed=2) -> dict` / `--second-account FIRST_OUT`, with a `second_account` block (`shared`,
  `clashing`, `album_clash`, `shared_hashes`).
- **For G (from E):** video sidecars are `kind: "sidecar"` with `sidecar_of` = the video, but
  `takeout.match_directory` only matches images. Stage 3 has to match sidecars for non-image
  files itself (reusing `takeout`'s candidate-name logic read only), or ask the lead. The
  corrupt JPEG has a sidecar; its date comes from that sidecar.
- **For G:** copy progress in bytes goes into `set_progress`'s `stage` text (for example
  `"Copying 1.2 GB of 3.4 GB"`); no contract change.
- **For I:** F shows the swap button right after Add to library is clicked. I must accept
  `swap_requested` only once the cleaned source's scan has finished (otherwise tell the user to
  wait), then confirm, then remove the old source. Raise an existing wizard instead of opening a
  second one.

### Decisions from wave 2 (lead, in progress)

- **H dates:** files in a `cleaned` source that aren't confirmed in the output database (path + hash)
  follow the plain rules, mtime included (§8.10 and plan win; L2 rewords §8.4 so the mtime ban covers
  dates from a Takeout only). Output DB opened `mode=ro&immutable=1`, or from a temp copy when a
  non-empty `-wal` exists; plain `mode=ro` is never used (it leaves `-wal`/`-shm` in the user's folder).
  Lookups on the coordinator; workers compute the plain date as fallback.
- **H review fixes requested:** album import and swap need a `completed` scan that ran after the run
  finished; rescan reapplies metadata when the output has new done runs; UNC-safe URI helper; album
  names normalised before the clash check, one bad album never aborts the import; shared match
  helper; chunked path lookup.
- **For G (from H):** `finalise_run` must give in-output kept rows the best date from a later run's
  duplicates too; the file stays where it is on disk.
- **For I:** wire Add to library *and* plain Add source of a cleanup output to H's
  `add_cleaned_source`, scan, then `import_run_albums`, then the swap offer (`swap_ready`). Today
  Add source of an output scans it but creates no albums.
- **Merge order changed:** H merged before G, so G reuses H's `open_output_database` for its
  read-only output check.
- **H accepted:** `recompute_dates` raises `FileNotFoundError` when the output DB is missing (a
  plain-rules recompute would re-date every file by mtime); the scan uses plain rules with a warning.
  `swap_ready(conn, cleaned_id, output_db, run_id)` needs a completed scan started after the run's
  `finished_at`. Rescans reapply metadata when the output has new done runs (settings marker).
- **G review fixes requested:** linear `run_duplicates`; `open()` enforces every folder refusal;
  only `<target>.part` of pending copies removed; `error` copies retried on resume; renames stored
  at plan time in output `settings`; one path comparison for resume; optional extension filter on
  `takeout.match_directory` (lead-approved minimal change).
- **G accepted:** `CleanupPipeline(..., main_sources=db.list_sources(main_conn))`, a snapshot taken
  on the main thread (**I must pass it**, or `open()` can't refuse a library source as output).
  New `check_folders_for(sources, …)`, `unfinished_run(runs, input)`; `connect_readonly` removed.
  `takeout.match_directory(..., is_media=None)` (default unchanged). Failed copies are retried on each
  `copy()`; only failures left at finalise are dropped. Renames kept in output `settings`
  (`cleanup_renames:<run_id>`, not cleaned up yet; harmless). One pipeline per thread
  (`check_same_thread`): I uses one for analysis and a new one for copying.
- **Video filename dates (author decision, 2026-11-07):** `VID_YYYYMMDD_HHMMSS` is now a PRD §8.4
  filename pattern (`dates.py`), so Android videos get a filename date. E's ground truth for
  `VID_20040402_080000.mp4` was right; G's test workaround that dropped such dates is removed.
- **L2:** README must explain the `cleaned` source type and why Change type is hidden for it.

### Decisions from wave 3 (lead, in progress)

- **I review fixes requested:** results go only to the wizard that started the job (input/output/run id
  taken from the finishing job); Cancel drops a queued job; after **every** completed scan of a
  `cleaned` source, `import_run_albums(..., None, ...)` runs (idempotent), so a cancelled first scan
  never loses albums; a refused add hides the swap button; wizard Add to library runs the normal
  overlap check; Open folder opens the folder; "Stopping cleanup…" on close.
- **Lock scope (lead):** during a cleanup, sources, Add source, Rescan and album actions are locked;
  people filter and sidebar setting saves stay enabled (the cleanup never writes the main DB).
- **I accepted:** a job asked for while the last thread is ending is queued; wizard is
  `WA_DeleteOnClose` and a closed one is replaced by a fresh one; the swap offer appears
  automatically after the cleaned scan and stays on the wizard's button. Plain Add source of an
  output can't offer a swap (no input, no run id). `sources_dialog.py` locks on `busy`.
- **J review fixes requested:** the pipeline input is `<out>/Takeout` (the first runs included the
  generator's manifest.json, so their stage 3 and 6 numbers are void); counts come from
  `pipe.inventory()`; a count mismatch on reuse stops the run; `remove_tree` needs a marker
  (`.imagory/imagory.db`, `imagory.db`, `Takeout/`) as well as being inside the scratch folder.
- **J size (author):** 50,000 photos instead of 100,000 (the author stopped the 100k run). PRD §10
  gets the 50k WSL figure, labelled as such.
- **J numbers for PRD §10 (WSL2, 8 CPUs, 7 workers, ~300 MB free RAM; not Windows), 50,000
  photos (49,517 scanned images, 92,533 non-Trash files, 953 Trash):** stage 2 113.7 s
  (436 photos/s), stage 3 8.3 s, plan + preview 21.4 s, stage 6 copy 161.9 s (47,288 copies,
  292/s; varied 162-170 s between runs), stage 7 finalise 63.9 s; pipeline 369 s (about 6 min).
  0 failed, 0 unaccounted, safe to delete. Generation 147 s (not in the pipeline).
- **Open for L2:** README for the wizard, Add to library and the swap; closing the app mid-copy waits
  for the current file.

## Wave 1

### A schema and SQL (Opus)
Why Opus: this migration runs on every existing library and has to rebuild `sources` while
foreign keys are pointing at it.
- [x] Migration 5: rebuild `sources` so its CHECK allows `'cleaned'`, using SQLite's 12-step procedure. `migrate()` turns `foreign_keys` off before migration 5, runs `PRAGMA foreign_key_check` after it, and turns foreign keys back on. Add `cleanup_runs`, `file_origins`, `extra_files`, `cleanup_copies` and their indexes as in the plan. Update `SOURCE_TYPES`
- [x] Migration tests: a version 4 database with sources, files, photos, albums and people upgrades with every row and link intact, `imagory.db.bak` written, `foreign_key_check` empty, a fresh database at version 5
- [x] `# --- cleanup ---` SQL: create, resume and finish runs; insert and list extra files; insert origins; insert copies, list pending ones, mark done or error; one `finalise_run` transaction (re-point the kept file rows to the `cleaned` source with new path, size and mtime; set `taken_at_local`, `taken_at_utc` and `date_source` to NULL where `date_source = 'mtime'` and recompute those photos; move extra files; create albums; delete the temporary source's leftover rows). **`file_origins.file_id` / `extra_file_id` always point at the kept row** (the one that survives in the `cleaned` source); each copy's own input path lives in `original_relative_path`, so deleting the leftover rows never loses provenance; export queries returning rows for `ExportFile`, `ExportAlbum` and `ExportRun`
- [x] Unit tests for every query against a temp database, including: a hash with three input copies has three origins after finalise, all pointing at the surviving row; an mtime-only file ends up undated. Report the function names and signatures, which G and H build on

### B planning functions (Sonnet)
Pure functions in `imagory/cleanup.py`: no Qt, no SQL, no file I/O.
- [x] `choose_kept(candidates) -> dict[hash, Candidate]`: in-output first, then year folder (`album_folder is None`), then non-album, then shortest path, then path order
- [x] `target_folder(taken_at_local) -> "YYYY/MM" | "Unknown date"`
- [x] `name_for(candidate) -> str`: title rules (extension mismatch, `.jpg`/`.jpeg` equal), the `-edited` rule (reuse `takeout`'s edited-suffix logic read only, or copy the rule and say so), and the on-disk name when there's no title
- [x] `sanitize_name(name) -> str`: Windows-invalid characters, control characters, trailing dots and spaces, reserved names, empty results
- [x] `resolve_clashes(...)`: casefold, against planned and existing output names, ` (n)` suffix; `shorten_for_max_path(output_root_len, rel) -> rel` at 250 characters, keeping the extension
- [x] Companion rule: a same-stem file in the same input folder with no sidecar and no date follows its photo's folder and final stem, including any ` (n)` clash suffix the photo got (test: two clashing HEIC+MOV pairs stay paired)
- [x] `build_plan(candidates, existing_output_paths, output_root_len) -> Plan` (JSON files map to `.imagory/takeout-json/<input path>`, not deduplicated). `estimate_space(plan, candidates) -> int` (bytes plus 5%). `taken_mtime_ns(taken_at_local, tz) -> int` with an injectable zone, as in `dates.py`
- [x] Tests: every rule, plus the odd titles in **Expected names** (results must match that table exactly), and plan results that don't depend on input order

### C copy-and-verify primitive (Sonnet)
`imagory/cleanup_copy.py`. Filesystem only, no SQL.
- [x] `copy_verified(src, dest, expected_hash, mtime_ns, cancel=None) -> CopyResult`: create parent folders, stream to `<dest>.part` in 1 MB chunks while hashing the source (BLAKE2b with 16-byte digest, the same as `thumbs.hash_bytes`, done incrementally), compare with `expected_hash`, re-read the `.part` and hash it, `os.replace` into place, set `mtime_ns` (or the source's mtime when it's None). On any failure, delete the `.part` and return `ok=False` with a short error. `src` is opened `rb` only
- [x] `already_done(dest, size, mtime_ns) -> bool` for resume, and `remove_stale_parts(folder)` that deletes only `*.part` files
- [x] Tests: success, the source changed (hash mismatch), dest folder read-only, cancel mid-file, leftover `.part` overwritten, a large file streamed (not read whole), and **the source is unchanged** after every test (`tests/fs_snapshot.py`)

### D `metadata.json` and report writers (Haiku)
`imagory/cleanup_export.py`. The spec is exact: follow it literally.
- [x] `write_metadata_json(path, runs, albums, files, generated_at)`: the format in the plan (`format`, `version: 1`, `generated_at`, `runs`, `albums`, `files`). Omit `None` fields for `kind: "other"` as the plan says. Use `json.dump` with `ensure_ascii=False` and `indent=1`. Write to `<path>.tmp`, then `os.replace`
- [x] `write_report(path, report: CleanupReport)`: plain UTF-8 text with a counts section, then one section per list, each with a heading and one path per line. Write it atomically too
- [x] Tests: round-trip with `json.load`; names with quotes, newlines and emoji survive; omitted fields; atomic write (no `.tmp` left behind); report sections present
- [x] If anything in the spec is unclear, stop and ask in the report. Don't guess

### E fake Takeout additions (Sonnet)
`scripts/make_fake_library.py --takeout`. Existing output and manifest fields must not change, so
current tests keep passing.
- [x] Add: videos (`.mp4`, `.mov`) with sidecars, an album folder with duplicate copies, `Archive`, `Trash`, a long name truncated on disk with the full `title`, titles containing `:` and `/`, two titles differing only in case, a corrupt JPEG, a motion pair (HEIC plus a MOV with no sidecar), an unused sidecar, an album `metadata.json` with a title, and an `-edited` copy
- [x] Manifest: a new `cleanup` block with **ground truth only**, not output paths (B owns the naming rules, so E must not re-implement them). For every input file it records: kind (`photo`, `other`, `json`, `sidecar`, `trash`), content hash group (which files are exact duplicates), sidecar `title`, the expected date (`null` when there's no sidecar, EXIF or filename date), album memberships (folder name, or the album JSON title), and whether it is a companion of another file
- [x] Tests in `tests/test_fake_library.py` (append only): the new files exist, and the manifest is consistent with the files on disk

### F wizard UI shell (Sonnet)
`imagory/ui/cleanup_wizard.py`. Built against the contracts with injected callables. It doesn't
call the pipeline itself.
- [x] Non-modal `CleanupWizard(QWidget or QDialog)` with the 6 pages from the plan's UI section. Constructor takes `check_input: Callable[[Path], tuple[bool, str]]` and `check_output: Callable[[Path, Path], FolderCheck]`
- [x] Signals: `analyse_requested(Path, Path)`, `copy_requested()`, `cancel_requested()`, `add_to_library_requested()`, `swap_requested()`, `open_folder_requested(Path)`, `open_report_requested(Path)`. Slots: `set_progress(stage: str, done: int, total: int)`, `show_preview(PreviewCounts)`, `show_done(DoneResult)`, `show_error(str)`
- [x] Closing during work asks to cancel. "Start copying" is disabled when `bytes_to_copy > free_bytes`. All sidecar-derived text is shown as plain text (`Qt.PlainText`), and the safe-to-delete wording matches the plan exactly
- [x] Reuse `theme.py` styles. Offscreen tests: page flow, refusal message shown, cancel prompt, the preview disables copying on low space

## Wave 2 (after A, B, C, D, E are merged)

### G pipeline orchestrator (Opus)
`imagory/cleanup_run.py`. It ties together A, B, C and D. It runs on a worker thread (I adds the
QThread), takes `progress` and `cancel` like `Scanner`, and never touches the main library
database except for one read-only check.
- [x] `check_folders(main_conn, library_root, input, output) -> FolderCheck`: every folder rule in the plan (nesting both ways, library folder, non-`cleaned` source refused, `cleaned` allowed, resume when the run's input matches, cloud-folder warning)
- [x] Stage 2: create the temporary `takeout` source in the output database and run the existing `Scanner` with output thumbnails. Don't edit `scanner.py`; if a hook is needed, ask the lead, because H owns that file
- [x] Stage 3: inventory walk inside `cleanup_run.py`, reusing `scanner.is_skipped_dir` and `takeout` matching read only. It sorts files into stored images, used sidecars, Trash, other files, unreadable images and non-sidecar JSON. Hashing runs in the worker pool. Writes `extra_files` rows with sidecar data. Date rule: sidecar, EXIF, filename, never mtime
- [x] Stage 4: build `Candidate`s (a `date_source == 'mtime'` from the Scanner counts as no date: `taken_at_local=None`), call `cleanup.build_plan`, write origins (with album folder, album title from album JSON) and copies. Stage 5: `preview() -> PreviewCounts`
- [x] Stage 6: copy pending rows through `cleanup_copy` in a small thread pool. The single DB writer marks results, and `remove_stale_parts` runs on resume. Stage 7: `finalise_run`, then `write_metadata_json` and `write_report`
- [x] "Safe to delete": every inventory file accounted for (plan rule). Return `DoneResult`
- [x] Integration tests on E's fixture, checking invariants against its ground truth: one output file per hash group, every input file accounted for, each file's folder matches its expected date, album sets match, Trash skipped, corrupt JPEG and unused sidecar copied, motion pair together, undated files in `Unknown date/` **and** `null` dates in both the output database and `metadata.json`, mtimes set, exact names only for the cases in **Expected names**, cancel during stages 2, 3 and 6 then resume with no duplicate rows and nothing copied twice, a second run (second fake account) merges and dedupes, an uncopied file blocks safe-to-delete, and **the input is unchanged** in every test

### H `cleaned` source in the main library (Opus)
Why Opus: this changes `scanner.py` and the date rules, and the swap must keep albums and people.
- [x] `scanner.py`: for a `cleaned` source, read per-file metadata from `<root>/.imagory/imagory.db` (opened read only, matched by relative path and confirmed by hash): dates (`takeout_json` kept), people, caption, favourite, upload time, location. Undated files stay undated (no mtime fallback). Files not in the output database follow the plain rules
- [x] `sources.py`: detect a cleanup output in Add source (`.imagory/imagory.db` present means `cleaned`), `add_cleaned_source`, `import_run_albums(main_conn, output_db, run_id)` with the `<name> (Takeout)` / `(Takeout 2)` clash rule, existing albums never changed, `recompute_dates` for `cleaned` reading the output database
- [x] `sources_dialog.py`: show the `cleaned` type and hide "Change type…" for it
- [x] Swap helper: remove the old Takeout source only after the cleaned one is scanned (uses the existing `remove_source`)
- [x] Tests (build output databases with A's SQL and fixtures in code): metadata carried over, undated stays undated, relocating the output folder keeps the metadata, album clash names, a swap keeps photo ids, dates, people and album memberships, a second run's output rescans cleanly, and the source folder is unchanged

## Wave 3 (after F, G, H are merged)

### I thread and main window wiring (Sonnet)
- [x] `ui/cleanup_thread.py`: a `CleanupThread(QThread)` that runs G's stages, emits progress, preview, done and error, supports cancel, and is the only writer of the output database
- [x] `main_window.py`: a **Clean up Takeout…** toolbar button next to Add source, plus the same action in the File menu. It opens F's wizard and connects it to the thread. Cleanup is disabled during scans; scanning, sources and album actions are disabled during a cleanup. **Add to library** uses H's helpers and the normal `ScanThread`, followed by the swap offer with confirmation. Open folder and Open report use the existing reveal helper
- [x] Offscreen tests: actions enabled and disabled correctly, a full small run through the wizard with the fixture, Add to library shows the photos in the grid with their people

### J cleanup benchmark (Haiku)
- [x] `scripts/bench_cleanup.py`: generate the 100,000-photo fake Takeout into a scratch folder, time stages 2, 3, 6 and 7 with G's API, and print a table. Set `IMAGORY_LIBRARY` to a scratch folder
- [x] Run it once on WSL **in the background** (it takes longer than the 10-minute foreground limit), with the fake Takeout and the output in a scratch folder outside the repo, and put the numbers in the report (the lead copies them to PRD §10, marked as WSL, not Windows)

## Wave 4: L2 final (lead)
- [x] Merge in order (W0 was merged in wave 0): A, B, C, D, E, F, then G, H, then I, J. After each merge, run full tests and ruff
- [x] Re-run the safety grep on `main`, and a read-through of every path that writes files
- [x] PRD §8.10, §10 and §16 brought up to date with the agents' reported decisions; README section (what cleanup does, originals never touched or deleted, disk space, offline promise); the plan's checkboxes; `docs/tasks.md` completed line
- [x] Windows handoff list for the author (needs real Windows, not WSL): long paths, reserved names, mtime on NTFS, the wizard's look in light and dark, and the `.exe`

## Rules for every agent

1. Read `CLAUDE.md`, `docs/takeout-cleanup-plan.md`, the PRD sections your task touches, and this
   file (by absolute path) before writing code.
2. Work only in your worktree: `git checkout -b <your branch> main`, then `uv sync`. Branch from
   local `main`, not the worktree's starting commit: worktrees start at `origin/main`, which may
   lack the wave 0 commits. Check with `git log -1 --oneline main` before writing code.
3. Follow `CLAUDE.md`: never write to, rename, move or delete anything in the input folder; no
   network; all SQL in `db.py`; one bad file never stops a run; `IMAGORY_LIBRARY` set in tests and
   runs; fixtures generated in code. Every test that runs on an input folder asserts it is
   unchanged with `tests/fs_snapshot.py`.
4. Don't add dependencies.
5. Before finishing: `uv run pytest -q`, `uv run ruff check .` and `uv run ruff format --check .`
   all pass.
6. Commit in focused Conventional Commits (scope `cleanup`, `db`, `scanner`, `ui`, …), each
   message ending with the `Co-Authored-By` line from your instructions. Don't push, merge, or edit
   lead-owned files.
7. If the contract or the plan is unclear or seems wrong, **stop and report**. Don't guess on
   anything that touches files on disk or the schema.
8. Final report: branch and commits, what was built, test results, public functions and their
   signatures, decisions for PRD §16, any change outside your files, and open issues.

## Status

| Task | Model | Status | Merged |
| --- | --- | --- | --- |
| L0 PRD and docs | Lead | done (fb58fd4) | yes |
| L1 contracts | Lead | done (7097070) | yes |
| W0 helpers | Haiku | done; lead added folder entries to the snapshot (67ee3b3) | yes (e3aed26) |
| A schema | Opus | done | yes (577a31b) |
| B planning | Sonnet | done | yes (1c3e92a) |
| C copy | Sonnet | done | yes (fd728c4) |
| D export | Haiku | done | yes (e2b2dfd) |
| E fixtures | Sonnet | done | yes (8d3b340) |
| F wizard | Sonnet | done | yes (2c38c0a) |
| G pipeline | Opus | done (review fixes 445849a) | yes (73501e7) |
| H cleaned source | Opus | done (review fixes f05af94) | yes (1f12583) |
| I wiring | Sonnet | done (review fixes aea0a12) | yes (968aeb9) |
| J bench | Haiku | done (review fixes 83d945c); 50k WSL numbers below | yes (0d55c6c) |
| L2 final | Lead | done (dcf8b95, e0c70b5); Windows handoff in docs/tasks.md | yes |
