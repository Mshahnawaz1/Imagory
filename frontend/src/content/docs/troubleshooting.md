---
title: Troubleshooting
description: Fixes for common problems, and where to find Imagory's logs and library.
group: Help
order: 1
---

## Where are my logs and data?

Imagory keeps everything in its library folder:

- **Windows:** `%LOCALAPPDATA%\Imagory\`
- **Linux:** `~/.local/share/imagory/`

It holds `imagory.db` (the database), `thumbs` (a thumbnail cache you can delete at any time) and `logs`. To open the logs, choose **Help**, then **Open log folder**. Logs stay on your computer and are never sent anywhere.

## Imagory does not start on Linux

Install Qt's system libraries. On Ubuntu or Debian:

```sh
sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1
```

You also need a desktop with X11 or XWayland.

## Windows says it protected my PC

See [Windows warnings](/docs/windows-warnings).

## Some photos are missing from the grid

- Photos whose files cannot be found are hidden, not deleted. Check that the drive is plugged in, then **Rescan all**.
- If you moved a folder, use **Sources…** then **Relocate…**. See [Sources](/docs/sources).
- Imagory reads JPEG, PNG, GIF and HEIC photos only. Videos and RAW files are not shown.

## A HEIC photo does not show

Imagory supports HEIC and HEIF. A file that is damaged or in an unusual variant may not open. It is counted as an error, and the scan carries on.

## The scan reports errors

One bad image never stops a scan. Imagory logs the problem, counts it as an error and moves on to the next file. The details are in the logs.

## Old test library is gone

A library made by a test build older than 0.2.0 is not carried over. Imagory starts empty and you add your folders again. Your photos are not affected.

## Photos from a Takeout have no people

People are read only from Takeout sources. If you added an extracted Takeout export as a plain folder, open **Sources…**, select it and use **Change type…** to make it a Google Takeout export. If names are still missing, remove the source and add it again. This changes nothing in your photo folder.

## Report a bug

Open an issue on [GitHub](https://github.com/Mshahnawaz1/imagory/issues/new). Say which version and system you use, what you did and what happened. You can attach files from the log folder. Please do not attach photos you want to keep private.
