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

- **Threads:** `convert_book()`, downloads and EPUB reading run on a `QThread` that lives for the whole job, never on the GUI thread. The engine's "stop when Tomecast dies" signal fires when the *thread* that started it exits, so the engine must be started from that long-lived thread. Workers talk to the GUI only through Qt signals (queued connections). QML never sees worker objects.
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
| `hardwareLabel` | string | e.g. `"CPU · 8 THREADS"`, then `"CPU · ABOUT 4.7× REAL TIME"` once a conversion has measured it |
| `errorMessage` | string | a problem to show in the error banner; `""` when there's none |

| Slot | Does |
|---|---|
| `setThemeSetting(name)` | switch the theme |
| `goTo(screen)` | move between screens that are allowed from here (back buttons) |
| `chooseBookFolder()` → string | the folder the file dialog should open in (last used, else `~`) |
| `dismissError()` | clear `errorMessage` |

### Book (drop and chapters screens)

| Property | Type | Meaning |
|---|---|---|
| `bookLoaded` | bool | |
| `loading` | bool | true while a book is being read |
| `bookTitle`, `bookAuthors` | string | authors joined with ", " |
| `coverSource` | string | `"image://dots/cover/<theme>?<n>"` (`<n>` changes per book), or `""` without a cover |
| `artSource` | string | `"image://dots/cantoria/<theme>"` |
| `formatsLabel` | string | the formats Tomecast can read today, from `tomecast.readers.FORMATS` joined with `" · "` (just `"EPUB"` for now) |
| `chapters` | ChapterModel | one row per section of the book |
| `skipFrontMatter` | bool | ticks and unticks the rows marked front matter |
| `selectionSummary` | object | `{sections, words, audio, make}`: ready-to-show uppercase strings, as in the mockup ("12 OF 15 SECTIONS", "26,371 WORDS", "ABOUT 2 H 47 MIN OF AUDIO", "ABOUT 36 MIN TO MAKE HERE") |

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
| `speed` | real | 0.75–1.25 in steps of 0.05 on the meter (saved between runs) |
| `previewText` | string | the first sentence of the first selected chapter |
| `previewPlaying` | bool | |
| `previewAvailable` | bool | false when no system audio player was found (the button explains) |

| Slot | Does |
|---|---|
| `setVoice(id)`, `setSpeed(value)` | |
| `togglePreview()` | voice `previewText` with the chosen voice and speed, then play it; again to stop |
| `startConversion()` | write `<book folder>/<book title>.m4b` (never overwriting: a number is added) and go to `"converting"` |

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
| `pauseConversion()`, `resumeConversion()` | |
| `cancelConversion()` | stops after the current sentence; the work is kept, so starting the same book again resumes |

### Done

| Property | Type | Meaning |
|---|---|---|
| `outputPath` | string | the file that was written |
| `audioLength` | string | `"2:47:00"` |
| `doneSummary` | string | `"OF AUDIO · 12 CHAPTERS · 79 MB · MADE IN 36 MIN"` |
| `warnings` | list of string | e.g. a damaged cover was left out |

| Slot | Does |
|---|---|
| `openOutputFolder()` | open the folder in the file manager |
| `convertAnother()` | back to `"drop"`, keeping voice and speed |

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

- Headless: `QT_QPA_PLATFORM=offscreen` (set in `tests/gui/conftest.py`), using pytest-qt's `qtbot`.
- The backend is tested with a fake engine (`tests/test_convert.py` has the pattern): a whole conversion through the backend, with pause, cancel and resume.
- `scripts/gui_screenshots.py` renders every screen in both themes to PNG, from a demo backend with Alice in Wonderland's real numbers, so the window can be compared with the mockups without a display.
