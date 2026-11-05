# Tomecast desktop window: design

*The visual design belongs to the maintainer, Ambrose Sigler. This file records his decisions.*

**For contributors and coding agents:** implement what's written here, and don't invent or restyle the look. If something isn't covered, or you think a change would help, propose it as options in an issue or pull request description and let the maintainer choose. *Last updated 2026-11-05.*

## Decisions so far

| Topic | Decision |
|---|---|
| Direction | **Night Studio**: a dark audio instrument. Near-black surfaces and one cool signal colour. Anything that measures sound or progress is built from dots. |
| Inspiration | The maintainer's own earlier "blueprint" site (one blue accent on near-black); Gradium Phonon's dot-matrix player; the print discipline of Nous Research's work. Tomecast is *inspired by* these, not a copy: it doesn't use Phonon's lime accent or the same artworks. |
| Themes | **Light and dark, following the desktop's setting**, with a manual switch in the app. Each is designed as its own palette, not an automatic inversion. |
| Motion | **One living element:** the sentence-dot grid while converting. Everything else animates only on state changes. |
| Artwork | **The book's own cover**, rendered in dots on the chapter, converting and done screens. **The drop screen**, where no book is loaded yet, shows **Luca della Robbia's *Cantoria*** (1431–38, marble children singing: a Renaissance image of voices) in the same dots. |

## Palette

| Role | Dark | Light | Notes |
|---|---|---|---|
| Background | `#0B0B0C` | `#F2F2F2` | |
| Surface (panels) | `#161618` | `#FFFFFF` | |
| Lines | `#2A2A2E` | `#D9D9DE` | 1 px hairlines |
| Text | `#F2F2F2` (17.6:1) | `#0B0B0C` | |
| Muted text | `#7E7E7E` (5.2:1) | `#5E5E66` | |
| Accent | `#7C8CFF` (6.6:1) | `#4A5CF2` (4.6:1) | The one signal colour: the current sentence, focus, primary actions |
| Waiting dots | `#2A2A2E` | `#D0D0D6` | Sentence grid, not yet voiced |

In the light palette, only the background, text and accent are confirmed. The other light values are placeholders until the maintainer picks them.

Body text stays at 4.5:1 contrast or better. Focus rings use the accent with real contrast.

## Type

All three are open-licensed (SIL OFL) and get bundled with the app.

| Use | Family | Rule |
|---|---|---|
| Big numerals and timers (`02:10:34`, `41%`) | **Doto** (dot-matrix) | Display sizes only, 24 px and up; unreadable smaller |
| Interface text | **IBM Plex Sans** | |
| Labels, data, file names, the live caption | **IBM Plex Mono** | Labels in caps with slight letter spacing |

## Motifs

- **The sentence grid** (the living element): one dot per chunk of text. Dim means waiting, text colour means voiced, accent means being spoken now. A chunk that fails its audio check blinks once while it's redone. Drawn as one custom item, so thousands of dots stay cheap.
- **A live caption** of the sentence being voiced, in Plex Mono.
- **No index tags:** no bracketed numbers like `[ 03 ]` on screens, rows or headers. Chapters are listed by title.
- **Dot meters** for amounts, like speed as an 11-dot meter.
- **A round play/pause** for the voice preview, with a row of dots as its waveform.
- **No boxes:** sections are separated by space, or a plain 1 px line where a list needs one. No dotted outlines and no crop-mark corners.
- **Images in dots (covers and art):** a classic halftone, drawn the same way as the art on the maintainer's site: round dots on a grid turned about 29° (so they never line up into rows and columns like a pixel screen), **3.4 px apart**, with each dot's *area* following the ink (radius = half the spacing × √ink). The biggest dots just touch, so every dot stays a whole circle. Each dot shows the average tone of its whole cell, after an autocontrast so dull covers still use every dot size. The image keeps its own shape. Dark theme: light dots on near-black, never inverted (inverted faces look like ghosts). Light theme: dark dots on paper. Drawn with Qt (no Pillow) once per book and size, not live. Fine line engravings don't survive this (their hatching is smaller than a dot), so artwork should be paintings or sculpture.

## Screens

1. **Drop:** a near-black field with no outline. While a book is dragged over the window, "DROP A BOOK HERE" turns the accent colour. Inside: the *Cantoria* in dots (no credit line on screen; the source is listed below and in `THIRD_PARTY.md`), "DROP A BOOK HERE", the formats that work today (only EPUB for now; others are added as their readers land) and Choose a book. The hardware result sits quietly in a corner ("CPU · about 4.7× real time").
2. **Chapters:** a spec-table layout (`[ 03 ]`, title, words, estimated minutes) with checkboxes and a dot meter for each chapter's length. The skip switches (page numbers, footnotes, front matter…) appear as toggles.
3. **Voice:** a voice menu grouped by language and gender ("American · female"), speed as a dot meter, and Preview with the round player.
4. **Converting:** time left in Doto, "4.7× real time", the sentence grid, the live caption, Pause, and the dotted cover.
5. **Done:** the full grid lit, the file name, length and size, and Open folder.

## Artwork sources

| Work | Where it's used | Source and license |
|---|---|---|
| Luca della Robbia, *Cantoria*, 1431–38 (Museo dell'Opera del Duomo, Florence) | Drop screen | [Wikimedia Commons: Cantoria Della Robbia OPA Florence 1.jpg](https://commons.wikimedia.org/wiki/File:Cantoria_Della_Robbia_OPA_Florence_1.jpg), public domain |

When the image ships with the app, add it to `THIRD_PARTY.md` too.

## Building it (Qt)

- **Qt Quick (QML)** through PySide6, with our own components on the Basic Controls style. Plain widget stylesheets can't do the dots.
- **Native window decorations.** Custom title bars break on tiling window managers and Wayland.
- **Resizes well.** Tomecast often sits in a small window in a corner of the screen. The same design reflows down to about 400 px wide: art and cover stack above the text, chapter rows drop the word count and length dots, the sentence grid takes as many columns as fit, and the big numbers and padding shrink. A separate compact 400 × 600 version is on the mockup canvas as an idea for a future mobile app; it is not the desktop layout.
- **Theme:** read the desktop setting (`QStyleHints.colorScheme()`, Qt 6.5+) and offer a manual override, because some setups (Hyprland and Omarchy, for example) may not report it.
- **Reduced motion:** respect GNOME's and KDE's animation settings, plus an in-app switch.
- **Light on resources while converting:** redraw only on progress events, never a continuous shader, since the voice engine needs the CPU. Animate state changes at 150–400 ms.
- **Fonts:** use static font files, loaded with `QFontDatabase.addApplicationFont`.
- **Accessibility:** give every custom control an accessible name.
- **The engine:** the window must start it from a thread that lives as long as the conversion (see `_stop_with_parent` in `engines/audiocpp.py`).
