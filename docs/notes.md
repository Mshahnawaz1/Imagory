
This note is for is about what this project does, what are the decisions I made during the process of working of this project and why? This is solemly to keep track of my decisions and organizing my plans. 

## Why build this?
I have been using google photos for years and due to that completely used up multiple accounts for photos. So I decided why not organize all these photos in local device. Also there was the issue of how google was continuisely asking me permission to get access to all of the google workspace to gemini which I obviosely didn't want to ~_~ . 
So to prevent that I downloaded my photos, but the issues with google takeout was it was very messy with the all the sidecars (json) of metadata. There are existing tools that clean up the takeout but all of them were either dev tools that required some knowledge or had a lot of bugs. 

So I wanted to have a clean program that can be used by non-programmers to view and organize the takeout photos along with device photos. For the photos organization initially I was thinking of adding face recognition model as in google photos and also if possible add in device recognition for objects (Ids, handwritten notes, etc). But that turns out will require more resource than I thought. So I left that for the future addition. 

Future: For the face recognition I decided to create a separate extention to my program to keep the main program smaller.

## What is being built?
For the initial version of `Imagory` I decide to just read the existing takeout folder, keep the information from the metadata store in `app-data`. It will scan a folder (plan or takeout) create data point for each image and then organize them based on their time. Some of the other features of `Imagory` are:
- Filter based on dates : Obviosely we need this in organization tool.
- A theme menu for light and dark mode. Added this because it was becoming issues to test light and dark mode by changing it via system setting :)
- Add photos to albums.
- Organizing based on peopes faces. (Taken the metadata from takeout for now)
- A search bar for names and peoples.

## Things I am proud of
- The speed of photos preview and scans turns out really good.
- The exprience to scroll through the images is very smooth and even better than the windows preview.
- The UI turned out good (will probably work more on it).
- The option to organize based on peoples data from the sidecar. This was left in the initial planning and only after the few iteration of `Imagory` I noticed the data in `json` of takeout, so now using it to categorize.
- The search based on peoples name. 

## How its being built
So for the coding assistant I used claude code with `opus 5.5` for majority of tasks (just because I had a lot of tokens). 
For the language I am using `python` cause that's what I am most familiar with and it gives pretty good results on the speed (tested on 100k generated images and it loaded in ~10 min). And using `pyside6` for the UI and windows. 
I am storing the images metadata using `SQLITE` (cause its the best) in the `APPDATA` 
. 
The work on this starts with the planning along with AI and creating a [PRD-docs](../PRD.md), and then refining it till all the components are clear.


## Things I learned
I learned a lot of how claude works, yes it is my first time using it and by no surprise it was used for most of the codes, And this project was made possible only because I got the free access to it. Thinking of buying it now, cause it worth its money. On the side note i might have blown about 500m+ tokens in few day. 

One thing I found really interesting while assigning a task is to use multiples agents to completed small parts of task on their own worktree and have them merge it after completion. This all were orchestrated by a main agent which monitor, review and runs the final tests. And interestingly you could tell the main agent to assign tasks to diffrent models based on the complexity (saves a lot of token if used correctly). 
After searching about it I found claude has its built-in feature named `/advisor`.

There is also the main thing when using AI assistant is to plan cleary. This thing need to be given more time than you think. And if you don't plan it properly, doesn't matter which best model you are using, its output is going to be shit.

Another thing I have to look into was how the licensing works, since I was going to make this project into MIT-licenced, I need to make sure I don't use any properitory codes and stuff. I still don't get it, tell me if you could.

I looked into the working of `sqlite3` and how awesome it is along with being very fast. Learned concepts like [indexing](indexing.md). 
Also am using `hash` for each images to find the duplicates.

Learned about how the .exe file compiler works (just overview), initially was pushing the code from `wsl` to the windows and their compiling into .exe file to test ~_~, kinda dumb way to do that. Didn't knew that I could just run it in the wsl without compiling. 


### Thougths and plans
Thinking of adding a feature to cleanup the takeout folder entirely and not just read from it. It will save disk space and also remove the duplicated (with user confirmation). 

Future plan would be to run face recognition model on the images using a separate extention which will be used by based app.

The immediate plan is to cleanup the UI to make it suitable for end users.


### References

``` bash
Table: 1 - Schema for database

sqlite> .schema
CREATE TABLE sources (
            id              INTEGER PRIMARY KEY,
            label           TEXT NOT NULL,
            root_path       TEXT NOT NULL,
            type            TEXT NOT NULL DEFAULT 'plain' CHECK (type IN ('plain', 'takeout')),
            type_overridden INTEGER NOT NULL DEFAULT 0,
            added_at        TEXT NOT NULL,
            last_scan_at    TEXT
        );
CREATE TABLE photos (
            id              INTEGER PRIMARY KEY,
            content_hash    TEXT UNIQUE,
            primary_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
            width           INTEGER,
            height          INTEGER,
            format          TEXT,
            taken_at_local  TEXT,
            taken_at_utc    TEXT,
            date_source     TEXT,
            latitude        REAL,
            longitude       REAL
        );
CREATE TABLE files (
            id                    INTEGER PRIMARY KEY,
            source_id             INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            photo_id              INTEGER REFERENCES photos(id) ON DELETE SET NULL,
            relative_path         TEXT NOT NULL COLLATE NOCASE,
            size                  INTEGER NOT NULL,
            modified_time         INTEGER NOT NULL,
            content_hash          TEXT,
            status                TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'missing')),
            taken_at_local        TEXT,
            taken_at_utc          TEXT,
            date_source           TEXT,
            sidecar_relative_path TEXT,
            first_seen_at         TEXT NOT NULL,
            last_seen_at          TEXT NOT NULL, exif_taken_at TEXT, exif_offset TEXT,
            UNIQUE (source_id, relative_path)
        );
CREATE TABLE scan_runs (
            id             INTEGER PRIMARY KEY,
            source_id      INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            started_at     TEXT NOT NULL,
            finished_at    TEXT,
            status         TEXT NOT NULL,
            found          INTEGER NOT NULL DEFAULT 0,
            added          INTEGER NOT NULL DEFAULT 0,
            updated        INTEGER NOT NULL DEFAULT 0,
            unchanged      INTEGER NOT NULL DEFAULT 0,
            missing        INTEGER NOT NULL DEFAULT 0,
            errors         INTEGER NOT NULL DEFAULT 0,
            skipped_by_ext TEXT NOT NULL DEFAULT '{}'
        , moved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
CREATE TABLE albums (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT NOT NULL
        );
CREATE TABLE album_photos (
            album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
            photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL,
            PRIMARY KEY (album_id, photo_id)
        );
CREATE INDEX idx_photos_taken_at_local ON photos(taken_at_local);
CREATE INDEX idx_photos_primary_file_id ON photos(primary_file_id);
CREATE INDEX idx_files_photo_id ON files(photo_id);
CREATE INDEX idx_files_content_hash ON files(content_hash);
CREATE INDEX idx_files_source_status ON files(source_id, status);
CREATE INDEX idx_files_source_hash ON files(source_id, content_hash);
CREATE INDEX idx_scan_runs_source_id ON scan_runs(source_id);
CREATE INDEX idx_album_photos_photo_id ON album_photos(photo_id);
```