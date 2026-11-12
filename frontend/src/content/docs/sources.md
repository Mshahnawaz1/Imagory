---
title: Sources
description: Add photo folders and Google Takeout exports, and rename, rescan, relocate or remove them.
group: Using Imagory
order: 1
---

A **source** is a folder you add to Imagory and give a name, such as "Phone backup" or "Account A Takeout". You can have as many sources as you like. They all appear together in one library.

## Add a source

Choose **Add source…** from the toolbar or the File menu (on an empty library, **Add a photo folder…**). Pick the folder and give it a name. Imagory scans it and its subfolders for JPEG, PNG, GIF and HEIC photos. Other files, such as videos and RAW files, are skipped.

You can add ordinary photo folders and **extracted** Google Takeout exports. Zip files from Google must be extracted first (see [Takeout import](/docs/takeout-import)).

## Source types

Imagory shows what it thinks the folder is, and you can change it.

- **Plain folder:** any folder of photos. Dates come from the photo itself (EXIF) or the file name.
- **Takeout:** a Google Takeout export. Dates, places, captions and people also come from the JSON files Google puts next to your photos.
- **Cleaned:** a folder made by [Clean up Takeout](/docs/takeout-cleanup). Imagory detects it by itself, and its type cannot be changed.

## Manage sources

Open **Sources…** (also in the sidebar as **Manage sources…**) to see each source with its photo and missing counts.

- **Rescan:** looks for new, changed and missing photos. Unchanged photos are skipped, so it is quick. **Rescan all** in the toolbar does this for every source.
- **Rename…:** changes the name Imagory shows.
- **Change type…:** switches between plain folder and Takeout. Imagory works out the dates again.
- **Relocate…:** use this when a drive letter changes or you moved the folder. Point the source at its new place and Imagory carries on without scanning from scratch.
- **Remove missing…:** makes Imagory forget the files it can no longer find in that source, after you confirm.
- **Remove…:** makes Imagory forget the whole source and its thumbnails, after you confirm.

Removing only affects Imagory's own database. **Your photos are never touched.**

## Missing files

If a file disappears from a source, Imagory hides it but does not forget it. It comes back if the file returns. This matters because an unplugged drive looks the same as deleted photos. If a whole source's folder is unavailable, Imagory tells you and still scans your other sources.

If you move a file to another folder inside the same source, Imagory recognises it, and the photo keeps its place and its albums.

## Duplicates

Identical files, with exactly the same contents, show **once** in the grid, even when they sit in different folders or sources. Imagory hides the extra copies and never deletes them. The viewer lists every place the photo was found.
