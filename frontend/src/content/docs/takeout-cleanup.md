---
title: Takeout cleanup
description: Make a tidy, deduplicated copy of a Google Takeout export without touching the original.
group: Using Imagory
order: 3
---

A Takeout export is messy: a JSON file next to every photo, the same photo in several album folders, cut-off file names, and file dates that are just the day you unzipped it. **Clean up Takeout…** (toolbar or File menu) makes a tidy copy in a new folder you choose.

> **Your original export is never changed, moved or deleted.** Imagory only reads it. The cleanup writes copies only into the output folder you pick.

## What you get

- **One copy of each photo or video.** Exact duplicates are copied once.
- **`YYYY/MM` folders** by the date the photo was taken, or an `Unknown date` folder when there is no real date.
- **The original file names** from Google Photos, made safe for Windows.
- **No JSON files** next to the photos. Dates, places, people, captions and favourites are kept in a database and in a plain-text `metadata.json`, both in a hidden `.imagory` folder inside the output. Albums are kept too.
- **A report** that lists everything skipped, renamed or not copied.

Videos and other files are copied too. The Trash folder is not copied, but it is listed in the report.

## Steps

1. Extract your Takeout zip files into one folder first.
2. Choose **Clean up Takeout…**, then pick the extracted export (the input) and an empty output folder. Or choose **Add source…**, pick the export and keep **Clean up first**: Imagory skips straight to the output folder and adds the copy to your library when it is done.
3. Imagory analyses the export and shows what it would copy: how many unique photos, how many duplicates, and how much disk space it needs (about the size of the photos plus 5%). Nothing has been copied yet.
4. Click **Start copying**. Every copy is checked against the original before it counts as done.

You can cancel at any time. Pick the same folders again and Imagory carries on where it stopped.

A second export, for example from another Google account, can be added to the same output folder. Duplicates are matched against what is already there.

## When is it safe to delete the original?

When the wizard says so. It says this only when every file in the export (except Trash) is accounted for in the copy. Otherwise it lists the files that are not. Imagory never deletes the export for you. That is always your decision.

## Add to library

On the last page, **Add to library** adds the output folder as a source of type *cleaned* and creates its albums. If an album name is already taken, the new one gets "(Takeout)" added. Photos keep their dates and people, and files with no real date stay undated instead of getting the unzip time.

If the original export was already a source, Imagory offers to remove it, after you confirm. Your photos keep their albums and people.

## Good to know

- Output folders in OneDrive, Dropbox, Google Drive or on a network share get a warning, because the output holds a small database.
- Like the rest of Imagory, the cleanup is fully offline.
- It has been tested on generated exports and not yet on many real ones. Keep your original until you have checked the result.
