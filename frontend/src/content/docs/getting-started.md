---
title: Getting started
description: Download Imagory, start it for the first time and add your first photo folder.
group: Getting started
order: 1
---

Imagory is a free photo browser that runs on your own computer. It is fully offline: it makes no network connections, so there is no telemetry, no account and no update check. It only *reads* your photos. It never changes, moves, renames or deletes them, and never writes anything into your photo folders.

## Download

Pick your system on the [download section of the home page](/#download).

- **Windows 10 and 11 (64-bit):** download `Imagory.exe`.
- **Linux (x86_64):** download `Imagory-<version>-linux-x86_64.tar.gz`.

All files are also on the GitHub releases page.

## Windows

1. Double-click `Imagory.exe`. There is nothing to install.
2. Windows may show a blue "Windows protected your PC" box, because Imagory is not code-signed. This is expected. See [Windows warnings](/docs/windows-warnings) for the two clicks that get past it.

## Linux

Imagory runs on Ubuntu 22.04 or newer, and other distributions of the same age, on a desktop with X11 or XWayland. The Linux build has had less testing than the Windows one.

```sh
tar -xzf Imagory-0.2.0-linux-x86_64.tar.gz
./Imagory-0.2.0-linux-x86_64/Imagory
```

If it does not start, install Qt's system libraries. On Ubuntu or Debian:

```sh
sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1
```

## First run

1. Start Imagory. On an empty library you see **Add a photo folder…**. Click it and choose a folder of photos, or an extracted Google Takeout export.
2. Give the folder a name, such as "Phone backup". Imagory checks whether it looks like a Google Takeout export and lets you change the type.
3. Imagory scans the folder and its subfolders in the background. Photos appear in the grid as they are processed. You can cancel at any time.
4. Double-click a photo (or press Enter) to open it. Use the **Left** and **Right** arrow keys to move between photos, and **Back to photos** (or Esc) to return.

Read on in [Sources](/docs/sources) to add more folders.

## Where Imagory keeps its data

Imagory stores its database, thumbnail cache and logs in its own library folder, never next to the program and never in your photo folders.

- **Windows:** `%LOCALAPPDATA%\Imagory\`
- **Linux:** `~/.local/share/imagory/`

The thumbnail folder (`thumbs`) is only a cache, so you can delete it at any time. Imagory makes the thumbnails again when it needs them.
