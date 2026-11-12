---
title: Windows warnings
description: Why Windows SmartScreen or your antivirus may complain about Imagory.exe, and how to continue safely.
group: Getting started
order: 2
---

Imagory releases are **not code-signed**. Code-signing certificates cost money each year, and this is a free, open source project. Because of this, Windows may be cautious about `Imagory.exe`.

## SmartScreen: "Windows protected your PC"

1. Click **More info**.
2. Click **Run anyway**.

You only need to do this the first time you run a new version.

## Antivirus warnings

Some antivirus tools flag unsigned single-file programs made with PyInstaller, which is the tool used to build `Imagory.exe`. This is a false alarm that many small programs run into. If yours blocks it, you can allow the file in your antivirus settings.

## How to check the download

- Download only from the [Imagory releases page on GitHub](https://github.com/Mshahnawaz1/imagory/releases), not from third-party sites.
- Imagory is open source. The code is public, and the app makes no network connections.
- If you would rather not trust the download at all, you can build Imagory yourself from the source code. The steps are in the repository on GitHub.

If you are still unsure, [the FAQ](/docs/faq) explains what the app does and does not do.
