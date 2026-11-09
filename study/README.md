# Study guides

This folder holds self-contained study guides. Each guide lives in its own folder, makes no network requests, uses no CDNs and has no build step.

The first guide is `geology-ch5/`, Chapter 5: Earthquakes and Earth's Interior, built from Quiz 9. It has key-concept notes, flashcards and a practice quiz with explanations.

## Opening a guide offline

Either of these works:

- **From disk:** download or clone this repository, then double-click `study/geology-ch5/index.html`. No server or internet connection is needed.
- **From the site:** visit `/study/geology-ch5/` once while online. A service worker saves the guide, so the same address keeps working offline after that first visit.

Service workers do not run on pages opened from disk, and they are not needed there because the files are already on your computer.

## Adding a new guide

1. Copy `study/geology-ch5/` to a new folder, for example `study/biology-ch2/`.
2. Replace `data.js` in the new folder with the new guide's content. Keep the same `window.STUDY_DATA` format. Questions can be split across `data.js` plus extra `data-N.js` files that push onto `window.STUDY_DATA.questions`. Each extra file must be listed in `index.html` (after `data.js` and before `app.js`), in `PRECACHE_URLS` in `sw.js` and in `DEFAULT_FILES` in `check-data.mjs`. If the new guide does not need one of the copied extra files, delete it and remove it from those three lists.
3. In the new folder's `sw.js`, change `CACHE_PREFIX` to a name unique to the guide, for example `'biology-ch2-'`. Guides on the same site share cache storage, so this keeps them from deleting each other's files.
4. In `manifest.webmanifest`, change `name`, `short_name` and `description`. Update any chapter-specific text in `index.html`.
5. If the guide does not have 25 questions, change `EXPECTED_QUESTIONS` at the top of the new folder's `check-data.mjs`.
6. From the repository root, run the new folder's check script, for example `node study/biology-ch2/check-data.mjs`.

When you edit a guide that is already published, bump the version in `CACHE_NAME` in its `sw.js`. Otherwise returning visitors keep seeing the saved copy.

## Checking the data

The check script needs Node and no other dependencies. From the repository root, run:

    node study/geology-ch5/check-data.mjs

With no arguments, the script loads `data.js` and the guide's extra `data-N.js` files in order and validates the combined data. To validate other data files, pass one or more paths, either absolute or relative to the folder you run the command from. They are loaded in the order given and held together to the question count set in the check script you run:

    node study/geology-ch5/check-data.mjs path/to/data.js

The script prints `OK: 25 questions, N concepts` and exits with code 0 when the data is valid. Otherwise it prints each problem and exits with code 1, including when a file is missing or cannot be parsed, and it names that file.
