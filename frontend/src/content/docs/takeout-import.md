---
title: Takeout import
description: Use a Google Takeout export in Imagory and see which dates, places and people it reads.
group: Using Imagory
order: 2
---

Google Takeout lets you download your Google Photos. Imagory can browse the export and use the extra information Google includes.

## Get your export

1. Request your Google Photos export at takeout.google.com.
2. Download the zip files when Google says they are ready.
3. **Extract all the zip files** into one folder. Imagory cannot read zip files yet, so they must be extracted first.
4. In Imagory, choose **Add source…** and pick that folder. See [Sources](/docs/sources).

## What Imagory reads

Next to each photo, Google stores a small JSON file. Imagory reads these from it:

- the **date** the photo was taken;
- the **place** (location), when there is one;
- the **caption** you wrote;
- whether it is a **favourite**;
- the **people** you tagged in Google Photos;
- when the photo was added to Google Photos.

Imagory only copies names that are already in the export. It does **no face recognition**. You can search and filter by person, and the sidebar has a People section.

## What is skipped

- The **Trash** folder is skipped.
- The Archive folder is imported like any other.
- Edited photos (`-edited`) are shown as photos of their own, with the date of the original.

## How dates are chosen

For a Takeout source, Imagory uses the first date it can find, in this order: the Takeout JSON, the photo's own EXIF date, a date in the file name (such as `IMG_20190131_235959`), and finally the file's modified time. For a plain folder, the Takeout step is left out.

Photos with no date at all go into an "Unknown date" section at the end of the grid.

## Tidying up

A Takeout export is messy, with a JSON file next to every photo and the same photo in several album folders. To get a tidy copy, see [Takeout cleanup](/docs/takeout-cleanup).
