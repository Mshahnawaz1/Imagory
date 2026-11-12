---
title: FAQ
description: Short answers to common questions about Imagory.
group: Help
order: 2
---

## Does Imagory connect to the internet?

No. The app makes no network connections: no telemetry, no accounts and no update checks.

## Does it change my photos?

No. Imagory only reads your photos. It never changes, moves, renames or deletes them, and never writes anything into your photo folders. The one feature that writes files is **Clean up Takeout**, and it writes copies only into an output folder you pick.

## Where does Imagory keep its data?

In its own library folder: `%LOCALAPPDATA%\Imagory\` on Windows and `~/.local/share/imagory/` on Linux. See [Troubleshooting](/docs/troubleshooting).

## Is it free?

Yes. Imagory is free and open source under the MIT licence.

## Which systems does it run on?

Windows 10 and 11 (64-bit) is the main target. A Linux x86_64 build for Ubuntu 22.04 or newer is also released, but has had less testing.

## Which photo formats does it read?

JPEG, PNG, GIF (first frame) and HEIC/HEIF. Videos and RAW files are skipped when browsing.

## Can I use my Google Takeout zip files directly?

Not yet. Extract them first, then add the folder. See [Takeout import](/docs/takeout-import).

## Does it recognise faces?

No. If you tagged people in Google Photos, Imagory copies those names from the Takeout files and lets you search by them. It does no face recognition.

## What happens to duplicates?

Identical files show once in the grid. The extra copies are hidden, never deleted.

## Why does Windows warn me about the download?

Releases are not code-signed. See [Windows warnings](/docs/windows-warnings).

## Will it delete my Takeout export?

Never. Cleanup copies into a new folder and leaves the original alone. You decide whether to delete it. See [Takeout cleanup](/docs/takeout-cleanup).

## How do I report a bug or ask for a feature?

Open an issue on [GitHub](https://github.com/Mshahnawaz1/imagory/issues).
