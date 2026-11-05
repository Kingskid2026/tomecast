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
  dots.py          render_dots(): images to Night Studio dots (pure Qt, no Pillow)
  dotgrid.py       DotGrid: the sentence grid as one painted QML item
  images.py        DotImageProvider: serves "image://dots/<name>/<theme>" to QML
  assets/          cantoria.jpg (source photo, public domain) and its credit
  fonts/           Doto, IBM Plex Sans, IBM Plex Mono static .ttf files plus OFL.txt
  icons/           tomecast.svg (app icon, placeholder until the maintainer approves one)
  qml/
    Main.qml       ApplicationWindow; shows the screen named by Backend.screen
    Theme.qml      singleton: palettes, fonts, spacing (follows Backend.theme)
    components/    buttons, meters, labels, the header, the error banner, the chapter row (see the folder)
    screens/       DropScreen, ChaptersScreen, VoiceScreen, ConvertingScreen, DoneScreen
```

**Imports:**
- Python registers its types under the URI `TomecastNative` 1.0: the `Backend` singleton (`qmlRegisterSingletonInstance`) and `DotGrid` (`qmlRegisterType`). QML files write `import TomecastNative`.
- The QML files are plain directory imports. `qml/qmldir` declares `singleton Theme 1.0 Theme.qml`. `Main.qml` writes `import "."`, `import "components"` and `import "screens"`; files in subfolders write `import ".."` (for `Theme`) and `import "../components"`.
- `app.py` loads `qml/Main.qml` by file path, after registering the Python types and the `dots` image provider.

## Rules

- **Threads:** `convert_book()`, downloads and book reading run on a `QThread` that lives for the whole job, never on the GUI thread. The engine's "stop when Tomecast dies" signal fires when the *thread* that started it exits, so the engine must be started from that long-lived thread. Workers talk to the GUI only through Qt signals (queued connections). QML never sees worker objects.
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
| `screen` | string | `"drop"`, `"chapters"`, `"voice"`, `"converting"` or `"done"` |
| `themeSetting` | string | `"system"`, `"dark"` or `"light"` (saved between runs) |
| `theme` | string | the effective theme: `"dark"` or `"light"` |
| `reduceMotion` | bool | true when animations should be skipped |
| `hardwareLabel` | string | what is voicing. `"CPU · 8 THREADS"` on the CPU, or the graphics card's short name (`short_name()`: `"NVIDIA RTX PRO 1000"`) when it is available, switched on and known. Once a conversion has measured the speed it ends `" · ABOUT 19.8× REAL TIME"` (`"CPU · ABOUT 4.7× REAL TIME"` on the CPU). The measurement is forgotten when `useGpu` changes, since it belonged to the other hardware |
| `gpuAvailable` | bool | true when `vulkan_possible()` was true at start: a Vulkan loader and a graphics device exist. `VoiceScreen.qml` shows the "Use graphics card" row only then |
| `useGpu` | bool | the "Use graphics card" choice, default true, saved between runs (`QSettings` key `useGpu`). It has an effect only while `gpuAvailable` is true |
| `gpuName` | string | the full description of the card that is (or would be) used, e.g. `"NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU"`; `""` until known. It is learned by a scan at start-up (only if the graphics-card engine is installed) and each time an engine starts, which also sets it back to `""` when the engine fell back to the CPU |
| `errorMessage` | string | a problem to show in the error banner; `""` when there's none |

| Slot | Does |
|---|---|
| `setThemeSetting(name)` | switch the theme |
| `setUseGpu(on)` | use the graphics card (through the driver already installed) or the CPU. Saved. Refreshes `downloadNote` and `hardwareLabel` |
| `goTo(screen)` | move between screens that are allowed from here (back buttons) |
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
| `formatsLabel` | string | the formats Tomecast can read today, from `tomecast.readers.FORMATS` joined with `" · "` ("EPUB · DOCX · FB2 · HTML · TXT" today). The file dialog's filter in `DropScreen.qml` must list the same suffixes as `tomecast.readers.SUFFIXES` (a test checks this). Books are read with `tomecast.readers.read_book`, which also picks the reader by content, so a mis-named book still opens once it's chosen |
| `chapters` | ChapterModel | one row per section of the book. A section with no title is called `"Untitled section"` (its accessible name is "Include untitled section") |
| `skipFrontMatter` | bool | ticks and unticks the rows marked front matter |
| `selectionSummary` | object | `{sections, words, audio, make}`: ready-to-show uppercase strings, as in the mockup. `audio` is the length of the finished book, so it shrinks as the speed goes up; `make` is the time to voice it, which stops shrinking above 1.5× because the voice never speaks faster than that. It is recomputed when the speed changes ("12 OF 15 SECTIONS", "26,371 WORDS", "ABOUT 2 H 47 MIN OF AUDIO", "ABOUT 36 MIN TO MAKE HERE") |

`ChapterModel` roles: `title` (without a leading "Chapter N."; rows show titles only, no index numbers), `wordsLabel` (`"2,181"`), `minutesLabel` (`"14 min"`, `"<1 min"`), `meterLit` (0–17 dots), `frontMatter` (bool), `selected` (bool, editable).

| Slot | Does |
|---|---|
| `openBook(url)` | read a book (a `file://` URL from the file dialog, or a dropped file), then go to `"chapters"` |
| `setChapterSelected(row, selected)` | |
| `setSkipFrontMatter(on)` | |

Front matter is guessed until the real cleanup lands (issue #4): sections before the first numbered chapter whose title looks like a title page, contents, copyright, dedication or edition note, or which have under 100 words.

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
| `downloadNote` | string | what Start will download: `""` when every file in `required_assets(use_gpu)` (the Vulkan build with the card on, the CPU build with it off, plus eSpeak and Kokoro) is installed, otherwise `"STARTING DOWNLOADS THE VOICE ONCE · 271 MB"` (the sizes of the *missing* assets added up, in MB, rounded up). Recomputed each time the voice screen is shown, when `useGpu` changes, and once a conversion's downloads have finished. A file already checked against its SHA-256 isn't hashed again until its size or modification time changes. `VoiceScreen.qml` shows it as a muted `MonoLabel` right above Start converting. |

| Slot | Does |
|---|---|
| `setVoice(id)`, `setSpeed(value)` | |
| `setOutputFormat(name)` | `"m4b"` or `"mp3"`; anything else is ignored. Saved |
| `togglePreview()` | voice `previewText` with the chosen voice and speed, then play it; again to stop. Above 1.5× the preview is also time-stretched, so it sounds like the book will (see the architecture notes on speed) |
| `startConversion()` | write `<book folder>/<book title>.m4b` (with `outputFormat == "mp3"`: the folder `<book folder>/<book title>/`) and go to `"converting"`. It does nothing while `loading` is true (Start is disabled then), because the book being read would replace this one. The job carries the **whole book** plus `chapters`, the indices of the ticked rows (see "Ticked sections and resuming" below). |

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

`convert_book()` calls `control.checkpoint()` after the last piece and right before packaging, so a Cancel that came during the last piece stops there (nothing is written, and "Stopped" is true). It then calls `control.commit()`: from that moment `ConversionControl.cancel()` and `pause()` return `False` and change nothing. The window uses the return value, so a Cancel that races the `"packaging"` stage message is refused instead of reporting "Stopped" while the book is written. Quitting the window during packaging waits for it to finish.

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

The jobs sent to the worker carry `use_gpu` (`useGpu` and `gpuAvailable`). The engine factory is `(data folder, voice, speed, use_gpu) -> Engine`; the real one is `AutoKokoro`, which falls back to the CPU itself and sets `engine.warning`, `engine.device_label` and `engine.gpu_description` once started. The worker reports the description through its `deviceChosen` signal, and adds the engine's warning to the conversion's `warnings` (so the done screen shows "The graphics card didn't work, so Tomecast used the CPU."). Previews use the card's engine only if it is already installed (previews never download), else the CPU's. The work folder is keyed by `use_gpu`, so a card run and a CPU run of one book never share chunks.

On `VoiceScreen.qml`, in the preview panel below the speed meter and only when `gpuAvailable`: a `CheckRow` (the same component as "Skip front matter") labelled "Use graphics card" (`useGpuSwitch`), and a muted `MonoLabel` (`gpuNote`): "FASTER · USES THE DRIVER ALREADY INSTALLED" when on, "USING THE CPU · THE GRAPHICS CARD IS USUALLY SEVERAL TIMES FASTER" when off.

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
