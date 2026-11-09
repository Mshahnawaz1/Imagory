# Imagory

Imagory is an offline photo browser over folders the user already has, including Google Takeout exports. It reads photos and never changes them.

## Library and sources

**Library**:
Imagory's own data folder, holding the database, thumbnails and logs. It is never inside a photo folder.
_Avoid_: Catalogue, app data

**Source**:
A folder the user added to the Library. Each Source has a type: plain, takeout or cleaned.
_Avoid_: Root, folder, location

**Plain source**:
A Source of ordinary photo folders, dated from EXIF, the filename or the file's modified time.

**Takeout source**:
A Source holding an extracted Google Takeout export, whose Sidecars supply dates, people and other metadata.
_Avoid_: Google source, export

**Cleaned source**:
A Source that is the Output of a Cleanup. Its metadata comes from the Output's own database. A user never picks this type.

**Scan**:
One pass over a Source that brings the Library up to date with what is on disk.
_Avoid_: Index, import, sync

## Photos and files

**File**:
One image file on disk inside a Source, recorded by its path relative to the Source.
_Avoid_: Copy, item, image

**Photo**:
One unique image, identified by its content hash. A Photo has one or more Files, possibly in different Sources.
_Avoid_: Picture, image, item

**Primary file**:
The File shown for a Photo: the first File that isn't missing, ordered by Source added date, then by first-seen date.
_Avoid_: Primary copy, main file

**Duplicate**:
A Photo with more than one File. Its extra Files are hidden, never deleted.

**Missing**:
A File no longer found on disk in its Source. A Photo is missing only when all of its Files are missing.
_Avoid_: Deleted, gone

**Taken date**:
The single best date of a File or Photo, together with its date source: Takeout JSON, EXIF, filename or modified time.
_Avoid_: Timestamp, created date

## Takeout

**Takeout export**:
The folder tree Google Takeout produces for Google Photos, with a Sidecar next to most images.
_Avoid_: Takeout zip (zips are out of scope)

**Sidecar**:
The JSON file Google Takeout writes next to an image, holding its metadata.
_Avoid_: JSON, metadata file

**Edited file**:
A File such as `IMG_1234-edited.jpg` that has no Sidecar of its own and uses the original's. It is a separate Photo.
_Avoid_: Edited copy

## Organising

**Album**:
A named set of Photos made by the user. It references Photos, not Files, so moves and renames never break it.

**Person**:
A name attached to Photos, read from Takeout Sidecars. A Photo has the union of the people on all its Files.
_Avoid_: Face, tag

## Takeout cleanup

**Cleanup**:
Making a deduplicated, metadata-preserving copy of a Takeout export in a folder the user picks. The originals are never touched.
_Avoid_: Clean, tidy, export

**Cleanup run**:
One Cleanup of one input folder, which can be resumed until it is finished.

**Output**:
The folder a Cleanup writes to, holding one File per unique Photo in `YYYY/MM` folders, plus its own database and `metadata.json`.
_Avoid_: Destination, target, export folder

**Copy**:
Writing one File from the Takeout export into the Output during a Cleanup. This is the only meaning of "copy".
_Avoid_: Using "copy" for one of a Photo's Files (say File)

**Origin**:
Where a kept File in the Output came from in the Takeout export, including every Duplicate it replaced.

**Extra file**:
A file a Cleanup keeps in the Output but that isn't a Photo: an unreadable image, a non-image file, or an unused Sidecar.
