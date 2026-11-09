# People from Takeout: tasks

Scope and design: `PRD.md` §8.9 (also §3, §8.3, §8.6, §8.7, §9, §16). This file tracks only this
feature. The project-wide list is `agents_docs/tasks.md`. Tick a box only when the item is
implemented, tested, and committed.

## Scope

In:
- Read `people`, `description`, `favorited`, `creationTime` from Takeout sidecars.
- Store people per photo (union across duplicate copies, never removed by a rescan).
- Show people, caption, favourite, and upload date in the viewer info panel.
- Search (text box) and filter (filter bar, chip) by person; the text box also matches captions.
- A "People" section in the sidebar's Library group: one entry per person with a photo count;
  clicking it shows that person's photos in the normal grid.

No backfill: photos scanned before this feature get names only after the source is removed and added again (development phase, old data may be wiped).

Out: face detection (V2), renaming/merging/deleting people, a Favourites filter, the Google
`url`, `imageViews`, device and app fields, Takeout zips, backfilling already-scanned sources.

Rules that must hold: photos and sidecars are only read; no network; all SQL in `db.py`;
migration 4 is a new numbered migration with the usual backup (released migrations are never edited); scans stay cancel-and-resume safe.

## Tasks

### 1. Parse and store
- [x] `takeout.py`: extend `Sidecar` and `parse_sidecar` with people (trim, drop blanks, dedupe ignoring case, cap length and count), description, favourite, upload time; unit tests with odd shapes (missing, wrong types, huge lists)
- [x] `db.py` migration 4: `people`, `photo_people`, `photos.description/favorite/uploaded_at_utc`, `photo_people(person_id)` index; migration test
- [x] `db.py`: `add_photo_people`, caption and favourite merge rules (first non-empty caption, any-copy favourite), `list_people` with visible-photo counts, `people_for_photo`
- [x] `scanner.py`: apply the new fields next to location; edited copies inherit people; integration test (duplicates in two folders merge; source folder unchanged)

### 2. Search and filter
- [x] `PhotoFilter.person_id`; SQL in `db.py` for filter, month counts, and count; combine with AND
- [x] Text search also matches person names and captions (each word matches path, person, or caption); tests
- [x] `photo_matches` for scan-added rows rejects person filters until the end-of-scan reload (as for albums)
- [x] Filter bar: person control and chip; tests

### 3. UI
- [x] Sidebar "People" section (list with counts, scrollable, shows "No people yet" when empty), click opens the filtered grid; disabled during scans
- [x] Info panel: People, Caption, Favourite, Added to Google Photos; escape all text (names are untrusted)
- [ ] Light and dark theme check for the new widgets (widgets reuse existing combo/list/chip styles; not yet checked by eye)

### 4. Tools and docs
- [x] `scripts/make_fake_library.py --takeout`: add people, captions, favourites to sidecars and the manifest
- [x] README: mention people and the one-off sidecar pass; keep the offline promise prominent
- [x] `agents_docs/tasks.md`: add one completed line once all of the above is done
- [x] Final check: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`

## Notes
- Windows-only checks (look, `.exe`) still need real Windows; not verified from WSL.
