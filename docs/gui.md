# The desktop window: how it's built

The look is defined in [design.md](design.md) and owned by the maintainer. This file is the engineering side: the module layout, and the **contract between Python and QML** that every part of the window follows. The mockups the window must match are the maintainer's Night Studio canvas (a local copy of its source lives in `local/mockups/canvas/project/*.dc.html` on the maintainer's machine; it isn't committed).

Run it with `tomecast gui [BOOK]`.

## Layout

```
src/tomecast/gui/
  __init__.py      run(argv) -> int, imported lazily by `tomecast gui` so the CLI never loads Qt
  app.py           QGuiApplication, fonts, theme, image provider, QML engine, loads qml/Main.qml
  backend.py       Backend: the one QObject QML talks to (registered as the singleton `Backend`)
  models.py        ChapterModel (QAbstractListModel) for the chapter list
  worker.py        ConversionWorker: runs downloads and convert_book() on a long-lived QThread
  fixes.py         FixLists: the book's and the global pronunciation list, as the Names screen edits them
  dots.py          render_dots(): images to Night Studio dots (pure Qt, no Pillow)
  dotgrid.py       DotGrid: the sentence grid as one painted QML item
  images.py        DotImageProvider: serves "image://dots/<name>/<theme>" to QML
  assets/          cantoria.jpg (source photo, public domain) and its credit
  fonts/           Doto, IBM Plex Sans, IBM Plex Mono static .ttf files plus OFL.txt
  icons/           tomecast.svg (the app icon, the sentence grid; see docs/design.md) and png/ (its PNG sizes, made by scripts/render_icons.py)
  qml/
    Main.qml       ApplicationWindow; shows the screen named by Backend.screen
    Theme.qml      singleton: palettes, fonts, spacing (follows Backend.theme)
    components/    buttons, meters, labels, the header, the error banner, the chapter row, LineField
                   (a field that is one 1 px line) and TagButton (the 36 px Names pills) (see the folder)
    screens/       DropScreen, ChaptersScreen, VoiceScreen, NamesScreen, ConvertingScreen, DoneScreen
```

**Imports:**
- Python registers its types under the URI `TomecastNative` 1.0: the `Backend` singleton (`qmlRegisterSingletonInstance`) and `DotGrid` (`qmlRegisterType`). QML files write `import TomecastNative`.
- The QML files are plain directory imports. `qml/qmldir` declares `singleton Theme 1.0 Theme.qml`. `Main.qml` writes `import "."`, `import "components"` and `import "screens"`; files in subfolders write `import ".."` (for `Theme`) and `import "../components"`.
- `app.py` loads `qml/Main.qml` by file path, after registering the Python types and the `dots` image provider.

## Rules

- **Threads:** `convert_book()`, downloads, book reading (with the name finder and reading the two pronunciation lists) and voicing previews run on a `QThread` that lives for the whole job, never on the GUI thread. Saving a change to a pronunciation list (a few hundred bytes) happens on the GUI thread, so a problem shows in the banner at once. The engine's "stop when Tomecast dies" signal fires when the *thread* that started it exits, so the engine must be started from that long-lived thread. Workers talk to the GUI only through Qt signals (queued connections). QML never sees worker objects.
- **Light on resources:** nothing animates continuously while converting. `DotGrid` repaints only when `done` changes; screen changes animate for 150–400 ms. A system "reduce motion" setting (or `Backend.reduceMotion`) turns animations off.
- **No new heavy dependencies:** PySide6-Essentials only (no QtMultimedia, no Pillow). The voice preview plays through the system's player (`pw-play`, then `paplay`, then `aplay`).
- **Accessibility:** every control has `Accessible.name`; keyboard focus is visible (2 px accent outline); text meets 4.5:1.
- **Native window decorations.**
- **One design that resizes well** (see `docs/design.md`). The window opens at 1200 × 780, is at least 400 × 560, and remembers its size. At 680 px wide and under, padding and gaps shrink, side-by-side parts stack, and chapter rows drop the word count and length dots. The sentence grid always fits: up to 70 columns of 5 px dots with 3 px gaps. **Every dot outside the dot art is 5 px at every width**: the grid, the meters, the waveform, and Doto's numerals, which are 67 px because Doto's dots are 0.075 em. Nothing scrolls sideways; tall screens scroll down. The mockups' `@container tc` CSS block is the spec.

## The `Backend` singleton (Python ↔ QML contract)

Every property has a `<name>Changed` notify signal. Lists are plain JS arrays of objects (`QVariantList` of `QVariantMap`), except `chapters`, which is a model.

### App

| Property | Type | Meaning |
|---|---|---|
| `screen` | string | `"drop"`, `"chapters"`, `"voice"`, `"names"`, `"converting"` or `"done"` |
| `themeSetting` | string | `"system"`, `"dark"` or `"light"` (saved between runs) |
| `theme` | string | the effective theme: `"dark"` or `"light"` |
| `reduceMotion` | bool | true when animations should be skipped |
| `hardwareLabel` | string | what is voicing. `"CPU · 8 THREADS"` on the CPU, or the graphics card's short name (`short_name()`: `"NVIDIA RTX PRO 1000"`) when it is available, switched on and known. Once the speed of that hardware is known it ends `" · ABOUT 19.8× REAL TIME"` (`"CPU · ABOUT 4.7× REAL TIME"` on the CPU): from the speed test at start-up (see "The speed test" below), then from what a conversion measured, which replaces it for the rest of the session. Each device keeps its own speed, so switching `useGpu` shows the other one's (or none) |
| `gpuAvailable` | bool | true when `vulkan_possible()` was true at start: a Vulkan loader and a graphics device exist. `VoiceScreen.qml` shows the "Use graphics card" row only then |
| `useGpu` | bool | the "Use graphics card" choice, default true, saved between runs (`QSettings` key `useGpu`). It has an effect only while `gpuAvailable` is true |
| `gpuName` | string | the full description of the card that is (or would be) used, e.g. `"NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU"`; `""` until known. It is learned by a scan at start-up (only if a graphics-card engine build is installed) and each time an engine starts, which also sets it back to `""` when the engine fell back to the CPU |
| `gpuNote` | string | the note under "Use graphics card". Until both the CPU's and the card's speeds are known: `"FASTER · USES THE DRIVER ALREADY INSTALLED"` (on) or `"USING THE CPU · THE GRAPHICS CARD IS USUALLY SEVERAL TIMES FASTER"` (off). Once they are, with the measured speed-up (card ÷ CPU, one decimal): `"ABOUT 4.3× FASTER HERE · USES THE DRIVER ALREADY INSTALLED"` or `"USING THE CPU · THE GRAPHICS CARD IS ABOUT 4.3× FASTER HERE"`, and when the card is under 1.05 times the CPU's speed, `"NO FASTER THAN THE CPU HERE · USES THE DRIVER ALREADY INSTALLED"` or `"USING THE CPU · THE GRAPHICS CARD WAS NO FASTER HERE"` |
| `errorMessage` | string | a problem to show in the error banner; `""` when there's none |

| Slot | Does |
|---|---|
| `setThemeSetting(name)` | switch the theme |
| `setUseGpu(on)` | use the graphics card (through the driver already installed) or the CPU. Saved. Refreshes `downloadNote` and `hardwareLabel` |
| `goTo(screen)` | move between screens that are allowed from here (back buttons, and Next): `"voice"` from chapters, done or names; `"names"` only from voice, and only when `names` isn't empty |
| `chooseBookFolder()` → string | the folder the file dialog should open in (last used, else `~`) |
| `dismissError()` | clear `errorMessage` |

### Book (drop and chapters screens)

| Property | Type | Meaning |
|---|---|---|
| `bookLoaded` | bool | |
| `loading` | bool | true while a book is being read |
| `bookTitle`, `bookAuthors` | string | authors joined with ", " |
| `coverSource` | string | `"image://dots/cover/<theme>?<n>"` (`<n>` changes per book), or `""` without a cover (a cover whose shape is thinner than 1:4 or wider than 4:1 counts as none) |
| `artSource` | string | `"image://dots/cantoria/<theme>"` |
| `formatsLabel` | string | the formats Tomecast can read today, from `tomecast.readers.FORMATS` joined with `" · "` ("EPUB · PDF · DOCX · FB2 · HTML · TXT" today). The file dialog's filter in `DropScreen.qml` must list the same suffixes as `tomecast.readers.SUFFIXES` (a test checks this). Books are read with `tomecast.readers.read_book`, which also picks the reader by content, so a mis-named book still opens once it's chosen |
| `chapters` | ChapterModel | one row per section of the book. A section with no title is called `"Untitled section"` (its accessible name is "Include untitled section") |
| `skipFrontMatter` | bool | ticks and unticks the rows marked front or back matter. On by default; not saved between runs |
| `bookHasPages` | bool | the open book is laid out in pages (a PDF), so it can have page numbers and running headers. `ChaptersScreen.qml` shows "Skip page numbers" only when this is true |
| `skipPageNumbers` | bool | leave out page numbers and running headers and footers (the `page-numbers` cleanup rule). On by default; not saved between runs |
| `footnotesAtEnd` | bool | read footnote text at the end of its chapter instead of leaving it out (`footnotes="end"`). Off by default; not saved between runs |
| `selectionSummary` | object | `{sections, words, audio, make}`: ready-to-show uppercase strings, as in the mockup. `audio` is the length of the finished book, so it shrinks as the speed goes up; `make` is the time to voice it, which stops shrinking above 1.5× because the voice never speaks faster than that. It is recomputed when the speed changes ("12 OF 15 SECTIONS", "26,371 WORDS", "ABOUT 2 H 47 MIN OF AUDIO", "ABOUT 36 MIN TO MAKE HERE") |

`ChapterModel` roles: `title` (without a leading "Chapter N."; rows show titles only, no index numbers), `wordsLabel` (`"2,181"`), `minutesLabel` (`"14 min"`, `"<1 min"`), `meterLit` (0–17 dots), `frontMatter` (bool: front or back matter, which "Skip front matter" unticks), `matterLabel` (`"FRONT MATTER"` before the story, `"BACK MATTER"` after it, `""` otherwise; the tag after the title shows it), `selected` (bool, editable).

| Slot | Does |
|---|---|
| `openBook(url)` | read a book (a `file://` URL from the file dialog, or a dropped file), then go to `"chapters"` |
| `setChapterSelected(row, selected)` | |
| `setSkipFrontMatter(on)` | |
| `setSkipPageNumbers(on)` | cleans the book again with the new switch and refreshes the chapter list (see below) |
| `setFootnotesAtEnd(on)` | the same, for footnotes |

**Cleanup.** The worker reads the book with `read_book()` and sends it as read (for a PDF, with its layout in `Book.raw`). The backend keeps that and runs `tomecast.cleanup.clean()` on the GUI thread, with `page_numbers=skipPageNumbers`, `footnotes="end"` if `footnotesAtEnd` else `"drop"`, and `skip=frozenset()`: front and back matter is found (`CleanupResult.matter`) but not removed, because "Skip front matter" unticks those rows instead, so they can be ticked again. Cleaning doesn't parse the PDF again, so it's quick: about 35 ms for a 360-page book (reading that book takes about 2.5 s). When `setSkipPageNumbers` or `setFootnotesAtEnd` changes a switch, the book is cleaned again and the rows replaced; a row that is still the same section (same position, title and front-matter flag) keeps its tick. `startConversion` sends the cleaned book. Front and back matter rules are in docs/architecture.md ("Cleanup").

The switches sit above the chapter list in a `Flow`, as in `Chapters.dc.html`: "Skip page numbers", "Skip front matter", "Read footnotes at chapter end", each a `CheckRow`, wrapping with 24 px between switches and 10 px between rows (the Flow's spacing is 10, and the first two switches sit in slots 14 px wider than themselves). Their object names are `skipPageNumbersSwitch`, `skipFrontMatterSwitch` and `footnotesAtEndSwitch`.

### Voice

| Property | Type | Meaning |
|---|---|---|
| `voiceGroups` | list | `[{label: "AMERICAN · FEMALE", voices: [{id: "af_heart", name: "Heart"}, …]}, …]` for American and British English |
| `voice` | string | the chosen voice id (saved between runs) |
| `voiceLabel` | string | `"HEART · AMERICAN · FEMALE"` |
| `outputFormat` | string | `"m4b"` (one audiobook file, the default) or `"mp3"` (a folder with one MP3 per chapter), saved between runs (`QSettings` key `outputFormat`; anything else falls back to `"m4b"`). `VoiceScreen.qml` shows it as two `ChoicePill`s (the pills the voice list uses), "M4B audiobook" (`format_m4b`) and "MP3 per chapter" (`format_mp3`), in a `Flow` (`outputFormatChoice`) under an OUTPUT label below the speed meter |
| `speed` | real | 0.5–3.0 in steps of 0.25 on the meter, 11 dots with 1.00× the third (saved between runs; an older saved value such as 1.05 snaps to the nearest step) |
| `minSpeed`, `maxSpeed`, `speedStep` | real | the meter's range (`MIN_SPEED`, `MAX_SPEED`, `SPEED_STEP` in `backend.py`). Constants, so no notify signal. `VoiceScreen.qml` reads its end labels ("0.5×", "3×") and dot values from them instead of hard-coding the range |
| `previewText` | string | the first sentence of the first selected chapter |
| `previewPlaying` | bool | |
| `previewAvailable` | bool | false when no system audio player was found (the button explains) |
| `downloadNote` | string | what Start will download: `""` when every file in `required_assets(use_gpu)` (the Vulkan build with the card on, the CPU build with it off, plus eSpeak and Kokoro) is installed (`needs_download()`), otherwise `"STARTING DOWNLOADS THE VOICE ONCE · 271 MB"` (the sizes of the *missing* assets added up, in MB, rounded up), or `"STARTING DOWNLOADS A VOICE ENGINE UPDATE · 45 MB"` when everything missing is a newer build of an engine that is installed (Start checks it before using it). An update that failed its self-check with this version of Tomecast isn't counted as missing. Recomputed each time the voice screen is shown, when `useGpu` changes, and once a conversion's downloads have finished. A file already checked against its SHA-256 isn't hashed again until its size or modification time changes. `VoiceScreen.qml` shows it as a muted `MonoLabel` right above Start converting. |

| Slot | Does |
|---|---|
| `setVoice(id)`, `setSpeed(value)` | |
| `setOutputFormat(name)` | `"m4b"` or `"mp3"`; anything else is ignored. Saved |
| `togglePreview()` | voice `previewText` with the chosen voice and speed, then play it; again to stop. Above 1.5× the preview is also time-stretched, so it sounds like the book will (see the architecture notes on speed). Like every player in the window it says the text as the book would: the pronunciation fixes, then the language's rules (what `tomecast preview` does), while `previewText` stays as written |
| `startConversion()` | write `<book folder>/<book title>.m4b` (with `outputFormat == "mp3"`: the folder `<book folder>/<book title>/`) and go to `"converting"`. It does nothing while `loading` is true (Start is disabled then), because the book being read would replace this one. The job carries the **whole book** plus `chapters`, the indices of the ticked rows (see "Ticked sections and resuming" below). |

`VoiceScreen.qml`'s primary button is **Next** (`voiceNextButton`, `goTo("names")`) when `names` isn't empty, and **Start converting** (`startConvertingButton`) when it is, so a book without names skips the Names step. `downloadNote` shows above whichever it is.

### Names

The Names step (design.md, "Screens") comes after Voice for books where the name finder found something. The worker computes the list when the book is read, with `find_book_names(book)` on the book as read (the same as `tomecast names`, but without leaving out words that already have a fix), and reads both pronunciation lists there too. Both arrive with the book in one `BookRead`.

| Property | Type | Meaning |
|---|---|---|
| `names` | list | `[{word: "Gryphon", count: 55, countLabel: "55×", meterLit: 7}, …]`, most frequent first. `meterLit` is 1–11 dots, log-scaled to the most frequent name (`round(log(count) / log(top) × 11)`, at least 1). Empty: no Names step |
| `nameFixes` | object | `{"Gryphon": "GRIFF-on"}`: for each listed name with a fix, the right side of the fix that applies to it (the book's list first, then the global one), as written in the list. A row's field shows it |
| `fixes` | list | YOUR FIXES: `[{word: "Gryphon", say: "GRIFF-on", scope: "book"}, …]`, one per left side, the book's (`"book"`) first, then the global ones (`"all"`) the book doesn't override, each in its file's order |
| `bookListName` | string | the book's list file name, `"alice.pronunciations.txt"` (for the footnote) |
| `playingName` | string | the name whose row is playing (or being voiced), `""` otherwise |
| `tryPlaying` | bool | the TRY ANY WORD text is playing (or being voiced) |

| Slot | Does |
|---|---|
| `setNameFix(word, say)` | save how to say a listed name. An empty (or blank) `say` removes its fix. A fix that exists is changed in the list that has it (keeping its left side, arrow, spacing and comment); a new one goes at the end of the book's list. Nothing is written if it's the same as before. A respelling the list can't hold (a phonetic spelling in /slashes/, or a "#" that would start a comment) is refused with a sentence in the banner |
| `removeFix(scope, word)` | remove the fix for `word` from the book's list (`"book"`) or the global one (`"all"`) |
| `toggleFixScope(scope, word)` | move the fix to the other list (the THIS BOOK / EVERY BOOK pill): it is written to the other file first, then removed from this one |
| `playName(word)` | voice `word` with the fixes, in the chosen voice and speed, and play it; again to stop |
| `playText(text)` | the same for any text (TRY ANY WORD) |

**Playing.** The preview, the rows' buttons and TRY ANY WORD share one path (`PreviewJob` on the worker, then the system player, as `togglePreview`): only one thing plays at a time, pressing another button switches to it, pressing the same one again stops it, and changing screen, voice or speed stops it too. Each voiced text is cached by voice, speed and the text as spoken, so a changed fix is voiced afresh.

**Saving** (`gui/fixes.py`). Each list is kept as its file's text. A change re-reads the file first (so lines added in an editor meanwhile are kept), applies `pronunciations.edit` and saves it with `pronunciations.save`: a temporary file next to it, moved into place, a symbolic link followed, the file's permissions kept, and a list left empty removed rather than kept as an empty file. If the book's folder (or the data folder) can't be written, the banner says so in a sentence ("Tomecast couldn't save alice.pronunciations.txt in /books (permission denied). The change works until you close Tomecast.") and the change is kept for the session. A list that exists but can't be read is never written. A fix change that works clears a banner an earlier fix change put up.

**Converting with the fixes.** `ConvertJob.fixes` is `None` normally: the worker reads the lists with `load_for_book(data folder, book)`, exactly as the command line does, and builds the `TextPrep` that `plan_chunks` and `convert_book` use. When a change couldn't be saved, the window passes its own lists instead, so the fix still reaches the book. Lines the lists can't use are added to the done screen's warnings ("Pronunciation list: alice.pronunciations.txt, line 3: …").

`NamesScreen.qml`: header buttons `namesBackButton` and `namesStartButton` (and `namesDownloadNote`, as on Voice); the table `namesTable` with rows `nameRow<i>` (`nameWord<i>`, `nameCount<i>`, `nameMeter<i>`, `nameField<i>`, `namePlay<i>`) and `showAllNames`; the side panel `namesSide` with `tryField`, `tryButton`, `tryWave`, `heardAs`, `fixesCount`, rows `fixRow<i>` (`fixText<i>`, `fixScope<i>`, `fixRemove<i>`) and `fixesNote`. A row's field saves on `editingFinished` (Enter, Tab or a click elsewhere) and then shows what was saved. After the THIS BOOK / EVERY BOOK pill or Remove rebuilds the list, keyboard focus goes back to the same fix's pill (or the Remove now in that place).

### Converting

| Property | Type | Meaning |
|---|---|---|
| `stage` | string | `"downloading"`, `"starting"`, `"voicing"`, `"packaging"` |
| `downloadLabel` | string | e.g. `"DOWNLOADING THE KOKORO VOICE MODEL · 45% OF 190 MB"` |
| `chunksDone`, `chunksTotal` | int | for the sentence grid and the percentage |
| `chapterLabel` | string | the chapter being voiced, in capitals: `"A CAUCUS-RACE AND A LONG TALE"` |
| `timeLeft` | string | `"00:30:32"`, or `"--:--:--"` until it's measurable |
| `progressLabel` | string | `"225 / 1,610 PIECES · 14%"` |
| `speedLabel` | string | `"4.7× REAL TIME"` (measured: audio made ÷ time taken) |
| `nowSpeaking` | string | the sentence being voiced |
| `paused` | bool | |

| Slot | Does |
|---|---|
| `pauseConversion()` | pauses before the next piece. **Only while `stage == "voicing"`**: during the download, the start-up and packaging it does nothing. |
| `resumeConversion()` | |
| `cancelConversion()` | stops after the current sentence; the work is kept, so starting the same book again resumes. **Not available while `stage == "packaging"`**: it does nothing then, because the audiobook is written regardless. |

Availability by stage (`ConvertingScreen.qml` disables the buttons to match; a paused conversion can always be resumed):

| `stage` | Pause | Cancel |
|---|---|---|
| `downloading`, `starting` | no | yes |
| `voicing` | yes | yes |
| `packaging` | no | no |

`convert_book()` calls `control.checkpoint()` after the last piece and right before packaging, so a Cancel that came during the last piece stops there (nothing is written, and "Stopped" is true). It then calls `control.commit()`: from that moment `ConversionControl.cancel()` and `pause()` return `False` and change nothing. The window uses the return value, so a Cancel that races the `"packaging"` stage message is refused instead of reporting "Stopped" while the book is written. Quitting the window during packaging waits for it to finish (`shutdown()` asks with `control.cancel()`, which refuses exactly when packaging has begun, so a commit can't slip in between looking and cancelling). At any other time quitting doesn't wait long: `shutdown()` stops every running engine at once (`stop_all_engines()`), the worker's waits stop when asked, and it waits at most `SHUTDOWN_WAIT_MS` twice over. If the worker is still busy even then (an engine stuck in a graphics driver, a stalled download), `app.finish()` writes the settings and leaves with `os._exit`: returning would let Qt destroy a running thread, which aborts with a core dump. The engines still get SIGTERM from the parent-death signal.

A book that finishes reading while a conversion runs (a drop that started before Start was pressed) is dropped: `_on_book_read` clears `loading` and leaves the converting screen, the book and the conversion alone.

### Ticked sections and resuming

`convert_book(book, engine, output, *, workdir, chapters=None, overwrite=True, ...)` takes the **whole** book and `chapters`, the indices into `book.chapters` to voice (`None`: all). Chunk IDs (`cNNN-…`) always use the chapter's index in the whole book, so the same chapter has the same chunk files whichever sections are ticked, and a run from the command line and a run from the window reuse each other's work. The M4B gets only the chosen chapters, in book order. Progress reports `chapter_index` as the position among the chosen chapters (so it indexes the converting screen's labels), and `chunks_total` counts only their chunks.

### Where the book is written

The window passes `overwrite=False` and `format=outputFormat`. For MP3, `startConversion` picks the folder `<title>` (or `<title> (2)`…) and `write_mp3s` claims the name with `mkdir` when it publishes, trying `(2)`, `(3)`… if somebody made it meanwhile; the folder is `ConversionResult.output`, and `ConvertResult.size_bytes` is the size of all its files. For M4B, `startConversion` picks `<title>.m4b` (or `<title> (2).m4b`…) if the name is free then, and `write_m4b` checks again when it publishes: it hard-links the finished file to the wanted name (which fails if the name exists), and tries `(2)`, `(3)`… if somebody saved a file there meanwhile. The file that was written is `ConversionResult.output`, which becomes `ConvertResult.output` and so `outputPath`. The command line keeps `overwrite=True`: `--force` decides there.

### Done

| Property | Type | Meaning |
|---|---|---|
| `outputPath` | string | the file that was written (for MP3, the folder), taken from `ConversionResult.output` (it may be `<title> (2).m4b` if the name was taken while converting) |
| `audioLength` | string | `"2:47:00"` |
| `doneSummary` | string | `"OF AUDIO · 12 CHAPTERS · 79 MB · MADE IN 36 MIN"`; for MP3, `"OF AUDIO · 12 MP3 FILES · …"` |
| `warnings` | list of string | e.g. a damaged cover was left out |

| Slot | Does |
|---|---|
| `openOutputFolder()` | open the folder in the file manager (for an MP3 run, the folder of MP3 files itself; for an M4B, the folder holding the file) |
| `convertAnother()` | back to `"drop"`, keeping voice and speed |

### The graphics card

The jobs sent to the worker carry `use_gpu` (`useGpu` and `gpuAvailable`). The engine factory is `(data folder, voice, speed, use_gpu) -> Engine`; the real one is `AutoKokoro`, which falls back to the CPU itself and sets `engine.warning`, `engine.device_label` and `engine.gpu_description` once started. The worker reports the description through its `deviceChosen` signal, and adds the engine's warning to the conversion's `warnings` (so the done screen shows "The graphics card didn't work, so Tomecast used the CPU."). Previews use the card's engine only if some build of it is already installed (previews never download, and use the build in use while an update waits), else the CPU's. The work folder is keyed by `use_gpu` and the engine build in use, so a card run and a CPU run of one book never share chunks.

On `VoiceScreen.qml`, in the preview panel below the speed meter and only when `gpuAvailable`: a `CheckRow` (the same component as "Skip front matter") labelled "Use graphics card" (`useGpuSwitch`), and a muted `MonoLabel` (`gpuNote`) showing `Backend.gpuNote`.

**Downloads, updates and the self-check.** Before converting, the worker runs `tomecast.downloads.prepare()` for the files the choice needs: pressing Start counts as agreeing to the downloads. It's skipped for the rest of the session once nothing is left to download or check; a download that failed, or a check with no verdict, is tried again at the next Start. `downloadLabel` reads `"DOWNLOADING ONCE (1 OF 2) · …"` for a first install and `"DOWNLOADING AN UPDATE · …"` for a newer engine build; the self-check of a new build runs during the `"starting"` stage. Waiting for another Tomecast that is installing, downloading and the self-check all stop when the conversion is cancelled or the window closes, and a stopped check records nothing. If the update didn't work on this computer, or couldn't be downloaded, the old build is used and the conversion's `warnings` start with *"The voice engine update didn't work on this computer, so Tomecast kept using the previous version."* (or "…couldn't be downloaded…"), shown on the done screen like the other notes. The engine is given the builds `prepare()` chose (`Engine.pin_builds`), and the work folder is keyed by them. If a just-switched build then fails on the book, it's rolled back (see architecture §11) and the error banner's message ends with that sentence and "Start again to make the book with it." (never while stopping or after Cancel). A finished book marks the builds as proven: after `converted` is sent, so the done screen never waits on another Tomecast's install, and not when the run fell back to the CPU (`Engine.fell_back`). The checks are injectable for tests (`Backend(engine_check=...)`: `(data folder, use_gpu, cancelled) -> SelfCheck | [VoiceCheck]`).

**The speed test.** At start-up the window sends one `ScanJob(gpu=gpuAvailable and useGpu)` to the worker: with "Use graphics card" off, Tomecast doesn't touch the card at all. Switching it on later sends another `ScanJob(gpu=True)` if the card's name or speed isn't known yet. The job lists the cards once (`Backend(device_scanner=...)`, `(data folder, cancelled) -> list[GpuDevice] | None`, `None` meaning there's no graphics-card build or it didn't answer), takes the card's name from that list, closes any preview engine, and calls the speed probe with the same list (`Backend(speed_probe=...)`, `(data folder, gpu, cancelled, devices) -> Speeds`; the real one is `tomecast.speed.probe`, with short time limits, a failure remembered for 3 days, and closing the window stopping it), so the cards aren't listed twice in one scan. That returns the CPU's speed and, with `gpu`, the card's: remembered in `speeds.json` per engine build and device, or measured now with the builds that are installed (a few seconds each, once per build and device; nothing is downloaded, and nothing is measured before the first conversion has installed the engine). The worker sends them back with `speedsKnown(Speeds)`, which fills `hardwareLabel`, `gpuNote` and `selectionSummary`'s "TO MAKE HERE" estimate. Until a speed is known the estimate uses 4.7× (`DEFAULT_SPEEDUP`). Because the worker runs one job at a time, a book opened during that first measurement is read once it's done.

## `DotGrid` (QML type from `dotgrid.py`)

```qml
DotGrid {
    total: Backend.chunksTotal      // one dot per piece of text
    done: Backend.chunksDone
    columns: 70
    dotSize: 5; gap: 3              // logical px; implicitWidth/Height follow
    doneColor: Theme.text; currentColor: Theme.accent; waitColor: Theme.wait
    complete: false                 // true: every dot in doneColor (the done screen)
}
```

One `QQuickPaintedItem`. It repaints only when a property changes and draws in device pixels, so it stays crisp at fractional scaling. Accessible role: progress bar, named "<done> of <total> pieces voiced".

## Gotchas found while building it

- **Register `Backend` before creating the QML engine.** With PySide6 6.12, calling `qmlRegisterSingletonInstance` after a `QQmlEngine` exists breaks that engine.
- **One QML engine per process.** A second engine doesn't see the shared singleton's change signals, so the tests share one window and reset the demo backend between tests.
- **No QML property may start with `on` plus a capital.** QML reads it as a signal handler, so the accent's text colour is `Theme.textOnAccent`, not `onAccent`.
- **Qt asks image providers for screen pixels** (`sourceSize` times the screen scale), and the dots provider draws for that scale.

## Testing

- Headless: `QT_QPA_PLATFORM=offscreen` (set in `tests/gui/conftest.py`, together with an empty `QT_QPA_PLATFORMTHEME` so the desktop's theme plugin can't leak a colour scheme in; the backend fixture also replaces `system_reduce_motion`, so the real `gsettings` never runs), using pytest-qt's `qtbot`.
- The backend is tested with a fake engine (`tests/test_convert.py` has the pattern): a whole conversion through the backend, with pause, cancel and resume.
- `scripts/gui_screenshots.py` renders every screen in both themes to PNG, from a demo backend with Alice in Wonderland's real numbers, so the window can be compared with the mockups without a display.
