# Tomecast architecture

*How Tomecast is designed, and why. Kept up to date as the design changes. Last updated 2026-11-05.*

## Goal

People who read ebooks want to listen to them too, without uploading books to a cloud service or paying a subscription. Tomecast is a simple Linux app that:

- reads the common ebook formats,
- understands what's actually *book text*, so it reads chapters and not "page 2", headers or footnote numbers,
- sounds human,
- runs well on anything from an old laptop to an NVIDIA or AMD gaming PC.

## Key decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Interface | Small desktop window **and** a terminal command, both on the same engine |
| 2 | Hardware | Works on CPU-only, NVIDIA, AMD and older laptops. **First launch scans the hardware and recommends settings.** A "Use graphics card" switch lets the user turn the GPU off, with a note that it's faster on |
| 3 | Formats | EPUB, TXT, HTML, DOCX, FB2, and normal text PDFs. Scanned PDFs are handled by the **optional AI pass** (PaddleOCR-VL-1.6), which only runs when a PDF has no real text, and asks first |
| 4 | Output | Just writes files, so people play them in any app. **M4B with chapters by default**, with a switch for one MP3 per chapter |
| 5 | Skip / clean | Page numbers, headers and footers, copyright page, table of contents, index, footnote markers (footnote text skipped or read at chapter end), links, image captions, tables, and "preview of the next book" excerpts |
| 6 | Smarts | Rules-based cleanup, plus an **optional local AI pass**: **Docling Heron** reads PDF page layout, **PaddleOCR-VL-1.6** reads scanned pages, and **LFM3-1.7B** makes judgment calls in any format (backup: MiniCPM5-2B) |
| 7 | Voices | One narrator voice (for now) |
| 8 | Extras | Pause & resume, preview voice & speed, pronunciation fixes |
| 9 | Install | One command on any distro (`uv tool install` / `pipx`). **The app sets up llama.cpp, espeak-ng, ffmpeg and the models itself.** Nothing to install by hand |
| — | Model sizes | **Quantized for speed and low memory** wherever quality holds up, with automatic fallbacks (section 10) |
| — | Name | **Tomecast** (PyPI `tomecast`) |
| — | License | **Apache 2.0** (so PDFs use `pypdfium2`, not the AGPL PyMuPDF) |
| — | Repo | **github.com/Kingskid2026/tomecast**, with high-quality open-source setup and automated releases (section 11) |
| 10 | Languages | English plus the big ones. Made-up words (Elvish etc.) get a best-guess pronunciation, and the pronunciation list fixes anything off |
| — | Voice models | **Kokoro** as the default that runs everywhere. **Ming-3.0-tts 0.5B** as the premium voice when a graphics card is available. Both run on **audio.cpp** (C++, no Python) |

## How it fits together

```
book file
  → Reader (one per format)          → chapters → blocks, with tags (heading, footnote, table, header/footer…)
  → Cleanup (rules, optional AI)      → only the text that should be spoken
  → Text prep                         → pronunciation list, numbers, abbreviations, sentence chunks
  → Voice engine (Kokoro or Ming)     → audio per chunk, cached on disk (this is what makes resume work)
  → Checks                            → every sentence has audio of the right length, or it's regenerated
  → Packager (ffmpeg)                 → M4B with chapters + cover + title/author, or MP3 per chapter
```

The app is written in **Python 3.12+** (uv downloads it if the system's Python is older), which has the best ebook and PDF libraries and holds everything together. The heavy model work runs in fast C++ engines (audio.cpp for voices, llama.cpp for the AI add-ons).

## Core guarantee: every sentence gets spoken

**The failure we're designing against:** some audiobook tools send a whole chapter or page to the voice model at once. Voice models silently stop when their input is too long, so the end of a page or chapter just goes missing, with no error. Tomecast never does that:

1. **Small pieces only.** Text goes to the voice in sentence-sized chunks, with a hard size cap for each engine, set well under that model's limit. Long sentences are split at commas or semicolons. A page or chapter is never sent in one call.
2. **Nothing left behind before the voice.** The splitter always emits the final fragment of a chapter, even without closing punctuation. That's a classic bug: splitters wait for a period that never comes. Readers keep the text after the last page break of a document.
3. **Every sentence is accounted for.** Each sentence gets an ID that follows it from the reader to the final file. Before packaging, the app checks that every ID has audio. A gap means regenerate, never ship.
4. **Length check.** Each chunk's audio length is compared with what its text should take to say. Audio that's too short, or silence where words should be, gets regenerated, split smaller on retry.
5. **Optional listen-back check.** A small speech-recognition model (audio.cpp runs several) transcribes the end of each chunk and compares it to the text. That catches dropped words that timing alone would miss. On by default with a graphics card, optional on CPU.
6. **Tests that prove it:** fixture documents whose last page ends mid-sentence, a chapter with no final period, and a 2,000-word paragraph. The tests check that every sentence of the cleaned text reached the audio.

## Components

### 1. Readers (one per format)
- **EPUB:** our own reader on `lxml` (BSD), with safe settings for untrusted files: no network, no entity expansion, and size limits against zip bombs. It follows the book's reading order and uses its table of contents for chapter titles. Many EPUBs tag their own parts (`epub:type="footnote"`, `pagebreak`, `copyright-page`, `toc`, `index`), so junk can be skipped exactly instead of guessed. Books with DRM-encrypted text are refused with a clear error. The cover image goes into the M4B. (We don't use `ebooklib`, because it's AGPL.)
- **PDF** (`readers/pdf.py`): `pypdfium2`, Apache/BSD-licensed, and the same PDF engine Chrome uses. We don't use PyMuPDF because it's AGPL, which doesn't fit an Apache 2.0 app. PDFium gives each character with its box, baseline and font size (times its text matrix, so text drawn at size 1 and scaled up still measures right), and the reader rebuilds the book from where the characters are:
  1. **Segments:** characters drawn one after another on the same baseline, close together. A gap wider than 1 em (a column gutter) starts a new segment.
  2. **Bands:** a short line on its own at the top or bottom fifth of a page (1.8 em or more from the text), or a line that's just a page number, is marked as a header or footer. Cleanup decides whether it's furniture.
  3. **Columns:** if most of the page's text is in narrow segments that leave a clear gap in the middle half of the page, the page has two columns. Lines that cross the gap (a title over both columns, a footnote under them) cut the page into bands, read top to bottom; inside a two-column band the left column comes first. So the drawing order doesn't matter.
  4. **Blocks:** lines are grouped into blocks (paragraphs) at a change of type size, a gap wider than the usual line spacing, an indented first line, an indented line after a short one (one-line paragraphs, dialogue), or a return to the margin after an indented quote. Small raised digits and symbols are footnote markers, wrapped in private-use brackets so cleanup can remove them. The first block of each column remembers that it may carry on the paragraph before it.
  5. **Roles:** small type (under 0.9 of the body size) at the foot of a column that starts with a marker is a footnote (and a small block without a label right after a page whose last note didn't end is its continuation). Text 1.15 times the body size or more is a heading. Three or more rows of three or more cells that line up are a table.
  6. **Chapters** come from the PDF's bookmarks (the top two levels; a bookmark that points partway down a page starts there), when at least two of them point at text. Headings at a bookmarked chapter's start that repeat its title are dropped, because the title is read first. Otherwise a chapter starts at a heading at least 1.25 times the body size, or at a line that reads "Chapter N" on its own; a run of headings makes one title ("Chapter One. The Gift"). While the pages before the first chapter are short (title page, copyright page, dedication), each is a section of its own, so cleanup can tell what each one is.
  - **Metadata** comes from the document info (title, and authors split at ";"); a title like "Microsoft Word - draft.docx" or "untitled" falls back to the file name. The **cover** is the first page rendered 600 px wide, as PNG, only when pictures cover at least half of it.
  - **Refused with a clear error:** encrypted PDFs ("protected by a password"), damaged files, PDFs whose text is mostly set sideways, and **scans**: pages that are pictures with no text layer ("Reading scanned PDFs (OCR) comes with an add-on"). A PDF where more than half the pages (and at least two) are text-less pictures counts as a scan, so scanned pages are never silently skipped.
  - The reader keeps this layout in `Book.raw` (blocks with their page, band, role and "may carry on" flag) and fills `Book.chapters` with every block as found, only the layout repaired. Cleanup rebuilds the chapters from `Book.raw`.
- **One entry point:** `read_book(path)` in `tomecast.readers` picks the reader. It looks at the file's contents where that's cheap (a zip holding `META-INF/container.xml` is an EPUB, one holding `word/document.xml` is a DOCX, a `<FictionBook>` root is FB2, a file starting `%PDF-` is a PDF), so a book with the wrong extension still opens, and otherwise goes by the extension. Anything else raises `InvalidEbookError` with a plain sentence. `FORMATS` (what the window lists) and `SUFFIXES` come from one table in `readers/__init__.py`, so a new format is one line there plus its reader.
- **DOCX:** `python-docx` (MIT) opens the file, but the text is read from the document's XML so that tracked insertions and text boxes aren't lost (deleted text is not read). Title and Heading 1 paragraphs start chapters, or Heading 2 when there is no Heading 1. Text in tables is kept as paragraphs in reading order, and headers and footers are skipped. The cover is the first inline picture, if it is a PNG or JPEG at least an inch across that isn't wider than tall and comes before the book's text. Title and authors come from the core properties. Zip-bomb limits are checked before anything is decompressed.
- **FB2:** `lxml` with the same hardened settings as the EPUB reader. Every titled `<section>` starts a chapter (nesting is flattened in reading order) and `<p>`, `<v>`, `<subtitle>` and table cells become paragraphs. Metadata and the `<coverpage>` image come from `<title-info>`. `<body name="notes">` (footnotes) is skipped for now. A file that defines XML entities at all is refused, which also stops "billion laughs" files and means a reference never silently loses text.
- **TXT:** decoded by byte-order mark, then UTF-8, then Windows-1252, then Latin-1, so decoding never fails, but a file full of control bytes is refused as binary. Lines like "Chapter 12", "CHAPTER TWELVE", "Chapter XII. Title" and "Part One" start chapters; bare capital roman numerals ("XII") do too when at least two appear and no "Chapter" lines do. Blank lines separate paragraphs and wrapped lines are re-joined. A file with no chapter lines is one chapter titled by the book's title (the file name). Project Gutenberg's license text outside the `*** START` and `*** END` markers is dropped.
- **HTML (a single `.html`, `.htm` or `.xhtml` file):** the same markup walker as an EPUB chapter document, shared in `readers/_markup.py` (scripts, styles, page breaks and Gutenberg boilerplate skipped). `<h1>` and `<h2>` start chapters.
- **Later, if wanted:** Kindle MOBI/AZW3 (DRM-free only) through Calibre's `ebook-convert` when Calibre is installed.

### 2. Cleanup: "read the book, not the page"
`tomecast.cleanup.clean(book, options)` turns a book as read into the text to speak. It's format-agnostic: a book with a layout (`Book.raw`, from the PDF reader) is rebuilt from its blocks, and any other book is cleaned paragraph by paragraph. Each rule is a named switch in `CleanupOptions`:

| Rule (`Removal.rule`) | Switch, default | What it removes |
|---|---|---|
| `page-numbers` | `page_numbers=True` | Header and footer lines (see the reader's bands) that are a page number ("12", "xiv", "- 12 -", "Page 12 of 300"), or whose words, with numbers left out, are in the same band on at least 3 pages (2 in a book of 4 pages or fewer). So "12 THE GIFT" on a left page and "THE GIFT 13" on a right page are one running header. Big text is never furniture: it's a heading. |
| `footnote-markers` | `footnote_markers=True` | Superscript markers ("Della¹ counted" reads "Della counted"), and a footnote's own label. |
| `footnotes` | `footnotes="drop"` | Footnote text: `"drop"`, `"end"` (read at the end of its chapter, after the chapter's text) or `"keep"` (where it was printed). A note split over two pages is joined first. |
| `links` | `links=True` | Web addresses (`https://…`, `www.…`, `domain.org/path`) and email addresses, keeping the punctuation after them and tidying empty brackets: "free (https://…)." reads "free.". A bare "Amazon.com" in a sentence stays. |
| `captions` | `captions=True` | Paragraphs of 80 words or fewer that start "Figure 3:", "Fig. 3.", "Table 2:", "Plate IV." and the like (a number and punctuation are needed, so "Figure 3 shows…" stays), or that are a bracketed "[Illustration: …]". |
| `tables` | `tables=True` | Blocks the reader marked as tables: skipped, never replaced by a sentence. With the switch off, each row is read as a paragraph. |
| front and back matter (`Matter`) | `skip=frozenset(Matter)`: each kind is its own switch | Whole sections: `front` (title page, dedication, edition notes, and sections under 100 words), `copyright`, `contents`, `index`, `also-by` and `preview` (another book's opening). They stay in the cleaned book, flagged in `CleanupResult.matter`; `CleanupResult.kept` lists the chapters to voice. |

Layout repair isn't a switch: `join_lines` rejoins a word hyphenated at a line end when the next line starts in lowercase ("pen-" + "nies"), keeps the hyphen before a capital, a digit or after words like "twenty" and "self" ("Anglo-Saxon", "twenty-one"), drops soft hyphens, and joins a line ending in a dash without a space. A block that may carry on (it starts flush at the top of a column or page, or in lowercase) joins the body block before it, unless that one ended a sentence and this one starts a new one, so paragraphs split by page and column breaks come back whole. Headings and kept headers and footers never join.

**Front and back matter** is found by title ("Contents", "Copyright", "Index", "Also by…", "Dedication", the book's own title) or by text (copyright notices, "Also by" lists, lines ending in page numbers for contents and index pages, "Read on for an excerpt…"). Front matter is looked for before the first numbered chapter ("Chapter 1", "IV."), back matter after the last one; a preview runs from its introduction to the end, and may start where the chapter numbers go back to 1. In a book without numbered chapters only the opening and closing runs count, and a book that would be all front matter has none. (This replaces the window's first guess, which only looked at the front.)

**Every rule logs what it removed** (`CleanupResult.removed`: rule, text, chapter and page), so tests check it, the command line prints a summary ("Skipping 2 sections: …", "Left out: page numbers and running headers and footers (8), …"), and the window can show it later.

Tests build PDFs from scratch (`tests/pdf_factory.py`, public-domain text only) and check known pages against the exact expected spoken text, plus a coverage test: the sample book set on short pages, with words, sentences and paragraphs split by page and column breaks, must keep every sentence of the story, in order.

**Where the switches are:**
- **Window:** above the chapter list, "Skip page numbers" (only for books with pages), "Skip front matter" (unticks the front and back matter rows, which stay listed so they can be ticked again) and "Read footnotes at chapter end". Changing the first or last cleans the book again and refreshes the list.
- **Terminal:** `tomecast convert` with `--keep-page-numbers`, `--keep-front-matter` and `--footnotes {drop,end}`.
- **Chapter list with checkboxes:** the window shows what it found, so the user can untick anything the rules got wrong *before* converting.

Not done yet: the other readers don't tag footnotes or captions (EPUB's `epub:type="footnote"` and `figcaption`, FB2's notes body), so for those formats the footnote switch has no effect and captions are found by their text only.

**Optional AI pass (off by default).** It handles two different jobs with different models. The hardware scan says which parts the machine can handle.

| Job | Model | How it works |
|---|---|---|
| **Messy PDF pages** (layout) | **Docling Heron** (IBM, 43M, Apache 2.0) | Looks at each page picture and labels boxes: page-header, page-footer, footnote, caption, table, section-header, document index. The words are taken **from the PDF itself** by box position, so nothing is retyped. Under a second per page on CPU. **Runs on ONNX Runtime** (full FP32, 170 MB), so it needs no PyTorch. Reading order comes from our own layout rules, using Docling's approach as the reference. |
| **Scanned or badly broken pages** | **PaddleOCR-VL-1.6** (Baidu, ~0.9B, Apache 2.0) | Reads the page picture and retypes it in reading order, with headers and footers marked. Runs through llama.cpp (official GGUF + vision file), so it works on NVIDIA, AMD or CPU. Only used for pages with no real text, or where Heron's result looks wrong. **4-bit by default. If a page comes back with too many words that aren't real words, that page is re-read at 8-bit.** The app shows a time estimate first, because it's slow on CPU. |
| **Judgment calls, any format** | **LFM3-1.7B** (Liquid AI, ~1 GB at 4-bit) · backup **MiniCPM5-2B** | Finds "About the author", next-book previews, and where chapter 1 really starts in a TXT or untagged EPUB. **It never retypes the book.** It gets numbered lines and answers keep/drop/chapter-start in a fixed JSON format (enforced by llama.cpp), and the app makes the edits, so it can't change the author's words. 256K context means it can see a whole chapter at once. Needs llama.cpp from 2026-10-28 or newer (or ONNX Runtime). License: LFM Open License, free for personal use and companies under $10M revenue. The model is downloaded on first use, so the app itself stays open source. |

Known issue to watch: reading order is the hard part. Even Docling's own reading-order step sometimes moves a list or footnote above a heading ([docling#4433](https://github.com/docling-project/docling/issues/4433)). The chapter-list review screen is the safety net.

### 3. Text prep
Text prep turns written text into speakable text, deterministically, in **one place**: `plan_chunks` in `convert.py`, between the Book and chunking. The window and the terminal command both go through it. Code: `textprep.py` (the framework and the safety net), `textprep_en.py` (English rules), `pronunciations.py` (the lists) and `names.py` (the name finder).

- **Sentence-sized chunks** come after it (section 4 and "Core guarantee"), so numbers are already words by the time the size cap is checked. That matters: long numbers are the usual reason a chunk hits the 510-phoneme limit.
- **English rules** (rules are per language; the book's language picks them, and a book with no language set is treated as English):
  - Numbers: cardinals up to a quadrillion (`1,250` → "one thousand two hundred fifty"), ordinals (`1st`), decimals, negatives, fractions (`3/4`, `1 1/2`, `½`), ranges (`5-7` → "five to seven"). Leading zeros and huge numbers are read digit by digit, and phone numbers digit by digit in groups.
  - Years only in year-like places: after *in, since, until, by, from, during, between...*, after *War of*, *Panic of* and similar, before AD/BC, in ranges (`1914-18`) and as decades (`1860s`, `'60s`). `1865` → "eighteen sixty-five", `1905` → "nineteen oh five", `2010` → "twenty ten". A bare `1500 men` stays a quantity: "one thousand five hundred men".
  - Dates (`4 July 1776` → "the fourth of July, seventeen seventy-six"; `July 4, 1776`; `2024-03-05`; US-style `12/25`), times (`3:45 p.m.`, `10 a.m.`, `14:30`), currency (`$5.50` → "five dollars and fifty cents", `£3`, `€10`, `$3.5 million`) and percentages.
  - Abbreviations: *Mr., Mrs., Ms., Dr., Prof., Rev., Gen., Capt., Lt., Sgt., Mt., Ft., Jr., Sr., Inc., Ltd., e.g., i.e., etc., vs., cf., et al., No. 5, p. 12, Fig. 3, Vol. 2, ch. 4, A.D., B.C.* The ambiguous ones use context: `St.` is Saint before a capitalised word (`St. Paul`) and Street after a capitalised name (`Baker St.`); `Dr.` is Doctor before a name and Drive after a street name; a title like `Gen.` is only expanded when a name follows. When an abbreviation's period also ends the sentence (`etc.`, `p.m.`), the full stop is kept so the chunker still splits there.
  - Roman numerals: after *Chapter, Book, Part, Act, Volume, Section...* (`Chapter XII` → "Chapter twelve"), after a name as a regnal number (`Henry VIII` → "Henry the eighth"), and as a bare chapter title (`XIV.` → "fourteen."). `Malcolm X`, `Vitamin C` and the pronoun *I* are left alone.
  - Letters in abbreviations are written out as sounds ("ay em", "pee em", "bee see"), because eSpeak reads a lone capital `A` as the word *a*.
- **Nothing is ever dropped.** Rules only replace a written form with words that say the same thing. On top of that, every paragraph is checked (`words_lost`): if a rule would lose a word, that paragraph is voiced as written. Tests cover every rule with exact expected output, plus a test that every input word survives or is a known expansion.
- **Other languages** plug in by registering an object with `heading()` and `body()` in `textprep.RULES`. Until then a book in another language is voiced as written (the pronunciation lists still apply).
- **Pronunciation fixes** are plain text files of *written → say it as* lines (the exact format is in the README):
  - A **global list**, `~/.local/share/tomecast/pronunciations.txt`, and a **per-book list** next to the book file (`Title.epub` → `Title.pronunciations.txt`). Next to the book was chosen over a folder keyed by the book's hash because it is easy to find and edit, and it travels with the book. The book's list wins over the global one. The same lists are used by the terminal command, and the window will use them too ([#10](https://github.com/Kingskid2026/tomecast/issues/10)).
  - Whole-word matching. A left side in lowercase matches any case; one with a capital matches that spelling and its ALL-CAPS version. Fixes are applied *before* the rules, so a fix can cover an abbreviation (`Dr. Who → the Doctor`).
  - **Right-hand sides are respellings, not phonemes** (see "Phonetic spellings" in section 10 for why). Capitalised syllables are lowercased before voicing because eSpeak spells all-caps words letter by letter.
  - A line the program can't use (no arrow, or an IPA `/value/`) is skipped with a warning that names the line.
- **Name finder** (`tomecast names BOOK`, and `names.find_book_names(book)` for the window): lists capitalised words that are used at least 3 times, were seen capitalised somewhere other than at the start of a sentence, are not in the common-word list and are never written in lowercase in the book (so "Table" is not a name). Counts are listed most frequent first, and words that already have a fix are left out. The common-word list (`data/common_words.txt`, about 22,000 words) is built by `scripts/make_wordlist.py` from public-domain Project Gutenberg books, so it has no license of its own.
- **Hear a word:** `tomecast preview "text"` voices the text with the chosen voice and speed and plays it (pw-play, paplay or aplay), or saves it with `-o file.wav`. It applies the same lists and rules as a conversion, so you hear what the book will sound like; `--raw` says it exactly as typed. There is no `--phonemes` option, for the reason below.
- **Made-up languages:** Kokoro sounds out unknown words with eSpeak NG's spelling rules (measured in section 10), so it reads *something* reasonable, and the list fixes the rest.

### 4. Voice engines
Both sit behind one small interface (`voices()`, `languages()`, `synthesize(chunk, voice, speed)`), so more engines can be added later.

**Both voices run on audio.cpp.** It's a C++ engine for voice models (Apache 2.0, v0.9.4, 4,000+ GitHub stars, updated daily) that runs Kokoro and Ming from GGUF files. It has official prebuilt Linux builds: CPU (64 MB) and Vulkan (116 MB).
- **No Python and no PyTorch.**
- **Vulkan means NVIDIA *and* AMD graphics cards work the same way.**
- Risk: it's young (first release June 2026) and run by one company, so we pin a tested version (v0.9.3 or newer, for Ming).
- **Checked facts (v0.9.4):**
  - Kokoro needs **eSpeak NG for every sentence**, not just unknown words. Tomecast downloads it as a separate runtime file (the `espeakng-loader` wheel, GPL-3.0) rather than as a Python dependency, and points audio.cpp at it. Its data path must stay under 160 bytes.
  - The prebuilt binaries need **glibc 2.43**, which is newer than Ubuntu 24.04, Debian 13 and current Fedora (see section 9 and [#18](https://github.com/Kingskid2026/tomecast/issues/18)).
  - Tomecast runs audio.cpp's **local HTTP server** once per book, so the model loads one time, and sends one chunk per request. That's about 30% faster than one process per sentence on short sentences.
  - There's no silent truncation: more than **510 phonemes** in one request is a hard error. Tomecast keeps chunks at **200 characters**, and if a chunk still hits the limit (long numbers, mostly), it splits that chunk and joins the audio.
  - Memory: about 1 GB while voicing, measured on a Ryzen AI 7 PRO 350 at about 4× faster than real time on 8 CPU threads.
- Fallback if it breaks: Kokoro through `kokoro-onnx` (FP16-stored weights, never the INT8 file, see section 10), and Ming through its official Python repo in a sandbox.

**Kokoro (default, every machine)**
- 82M parameters, Apache 2.0, 54 voices. Languages: English (US/UK), Spanish, French, Italian, Portuguese, Hindi, Japanese, Chinese.
- The English file is ~190 MB. Non-English books use a larger multilingual file (~0.93 GB), downloaded only when one shows up.
- Runs on CPU, or on any graphics card through Vulkan.
- Pronunciation of unknown words, phonetic spellings and Q8_0 vs BF16 are checked in section 10 ([#3](https://github.com/Kingskid2026/tomecast/issues/3)): Q8_0 stays.

**Ming-3.0-tts 0.5B (premium, graphics card)**
- MIT license. Built-in voices include **UK_George** (labelled "audiobooks") and **US_Emma** (labelled "narration"). Languages: English, Chinese, German, Spanish, French, Korean, Japanese. Output is 44.1 kHz.
- Uses the official audio.cpp **Q8_0** file (1.74 GB). Measured on an RTX 5090: about 6× faster than real time, using ~1.9 GB of graphics memory. audio.cpp also reports valid output on CPU and Vulkan.
- NVIDIA and AMD both run it through Vulkan. On CPU it's offered only if step-2 testing shows a usable speed.
- Our text-prep step replaces its built-in number/date normalizer.
- Installed only when the user opts in ("Install premium voice, about 1.9 GB, needs a graphics card").

**Language routing:** if a book's language isn't covered by the chosen engine, the app says so and switches to the one that covers it. Italian, Portuguese and Hindi go to Kokoro. German and Korean go to Ming.

### 5. Hardware scan (first launch, plus a "Rescan" button)
- Detects the graphics card (NVIDIA, AMD or Intel, plus its memory, and whether Vulkan works), CPU cores, RAM, and whether it's a laptop on battery.
- Runs a ~10-second test with each available engine, then shows a real estimate: *"This book will take about 2 h 10 min on your graphics card, or 6 h on CPU."*
- Recommends a setting:

| Machine | Recommended |
|---|---|
| NVIDIA or AMD graphics, 4 GB+ | Ming on the graphics card through Vulkan (Kokoro also available) |
| CPU only, decent RAM | Kokoro |
| Old or low-RAM laptop | Kokoro, fewer threads |

- The **"Use graphics card"** switch is always visible, with the measured speedup next to it ("about 4× faster on your machine").

**What is built so far (Kokoro on the graphics card, issue #8).** The full scan above is still to come; today's version is deliberately small and never touches drivers:

- `tomecast.hardware.vulkan_possible()` is a cheap check with no download: `libvulkan.so.1` can be loaded and a graphics device node exists (`/dev/dri/renderD*` or `/dev/nvidia0`). The window shows the "Use graphics card" row only when it is true, and `tomecast convert` defaults to the card only then.
- Which devices exist is asked of audio.cpp itself: `audiocpp_server --backend vulkan --list-devices --json` (the last stdout line that parses as JSON; `list_devices()` never raises, a failure means no devices). `pick_device()` takes the discrete `gpu` with the most free memory, else an `igpu` with at least 2 GB free, else nothing.
- **One engine build per choice.** Using the card needs only the Vulkan build (`AUDIOCPP_VULKAN`, 117 MB), which also contains the CPU backend. Not using it needs only the CPU build. So the required files are `required_assets(use_gpu)`: that build, eSpeak and Kokoro.
- **Fallback.** `AutoKokoro` (used by both the command line and the window) starts the Vulkan build on the picked device and checks that `/health` reports the `vulkan` backend. If no device is found, the server won't start, or it reports another backend, it raises `GpuUnavailable` internally and starts again with `"backend": "cpu"` on the same binary, with the warning "The graphics card didn't work, so Tomecast used the CPU." (or "No usable graphics card was found…"). The warning is shown as a note on the command line and on the done screen.
- **Resuming.** The work-folder key includes which engine build is used, so card and CPU audio never mix in one book.
- `tomecast doctor` prints the distro, glibc, CPU threads, `vulkan_possible()`, the Vulkan devices (once the Vulkan build is installed) and which engines and models are installed, for bug reports.
- Measured on the maintainer's laptop (Gift of the Magi, 13 min of audio, whole run including start-up and packaging): 41 s on the NVIDIA RTX PRO 1000 (about 19× real time) and 2 min 51 s on the CPU (about 4.6×).

### 6. Pause & resume, preview
- **Resume (built):**
  - Each conversion keeps its chunk audio in `~/.local/share/tomecast/cache/convert-<key>/`.
  - The key is a hash of the book file, the voice, the speed, the seed, and the engine and model versions, so a resumed book never mixes two voices or models.
  - Each chunk's file name includes a hash of its exact text, and a file only gets its final name once its audio has passed the checks. So stopping (Ctrl-C, `kill`, a crash, a reboot) loses at most the chunk in progress, and changed text is always voiced again.
  - Running the same command again picks up where it stopped. The folder is deleted after success, and unused ones after 14 days.
  - A lock stops two runs of the same book from colliding.
  - Chunk IDs use each chapter's index in the *whole* book, however few chapters are being voiced. So the command line (all chapters) and the window (the ticked ones) reuse each other's chunks, and changing the ticks voices only what is new.
  - Cancel and Pause work between chunks and once more just before packaging. Packaging itself can't be stopped: once it starts, cancelling is refused.
  - The engine process is stopped with Tomecast, even if Tomecast is killed.
- **Pause** in the desktop window will use the same mechanism.
- **Speaking speed (0.5× to 3.0×):**
  - Kokoro's own `speed` is linear up to about 1.5 and then saturates. Measured on one sentence (audio.cpp 0.9.4): 1.5 gave 1.55× faster, 2.0 only 1.87×, 2.5 2.06× and 3.0 only 2.12×.
  - So a requested speed is split in one place, `split_speed()` in `engines/audiocpp.py`: `voice_speed = min(speed, 1.5)` goes to the voice, and `tempo = speed / voice_speed` (at most 2.0) is left over.
  - The packager applies `tempo` as a pitch-preserving ffmpeg `atempo` filter inside the single encode, and divides every chapter mark by it. The preview gets the same stretch.
  - The "too little audio" check in `audio_problem` allows proportionally shorter audio at the voice's speed, and is unchanged at 1.0 and below.
  - The work folder's key includes the speed, so chunks voiced at one speed are never reused at another.
- **Preview:** play the first paragraph of any chapter, or any typed text, in the chosen voice and speed before converting.

### 7. Packager
- `ffmpeg` (available on every distro) joins the chunks.
- **M4B:** AAC, 64 kbps mono (about 30 MB per hour), chapter markers from the book's chapters, plus cover art, title, author and "narrator: <voice>".
- **MP3 per chapter** (`write_mp3s`, chosen with `--format mp3` or the window's output choice): a folder named after the book, one file per chapter, `NN - Chapter title.mp3` with NN at least two digits, and every file and folder name made safe for FAT32, exFAT and NTFS (`portable_name`), since USB sticks for car stereos are usually FAT32 or exFAT.
  - Each chapter is its own ffmpeg encode (libmp3lame, mono, the voice's 24 kHz, 64 kbps constant bit rate, about 28.8 MB per hour). CBR rather than VBR: the size is predictable, every player and car stereo seeks correctly, and speech gains little from VBR. Unlike the M4B there is no priming-delay drift to avoid, because each file stands alone and its length is its own chapter.
  - The audio of each chapter is exactly what the M4B has for it: the same segments, gaps, titles and `atempo`. The test suite checks every chunk is in exactly one file.
  - ID3v2.3 tags: title = chapter, album = book, artist and album artist = authors, track = `n/total`, composer = narrator, genre "Audiobook", plus the validated cover as the front-cover picture in every file.
  - Written to a hidden temporary folder, then moved into place. With `overwrite=False` the name is claimed with `mkdir` ("Title", then "Title (2)"...) so an existing folder is never touched, even one that appears while encoding. With `overwrite=True` (the command line's `--force`) only the old numbered MP3s in an existing folder are replaced.

### 8. Window and terminal command
- **Window (PySide6 / Qt):** works on KDE, GNOME and tiling setups.
  1. Drop a book in.
  2. Chapter list with checkboxes and the skip switches.
  3. Voice, speed and Preview.
  4. Pronunciation tab with the name finder and Try it.
  5. Convert, with progress, time left, and Pause/Resume.
  6. "Open output folder".
- **Terminal:** `tomecast convert book.epub --voice UK_George --format m4b`, plus `tomecast voices`, `tomecast preview "text"`, `tomecast names book.epub`, `tomecast scan`. Easy for a techy user to script over a folder of books.

### 9. Install: the app sets everything up itself
Users paste **one line** into a terminal: `curl -fsSL https://raw.githubusercontent.com/Kingskid2026/tomecast/main/install.sh | sh`. No `sudo`: everything goes into the user's home folder.
- It's a short script, kept in the repo, so a techy user can read it first.
- It installs **uv** if it's missing, and uv then fetches the right Python and installs the app.
- If they already have uv, it's just `uv tool install tomecast`.
- They never install llama.cpp, audio.cpp, CUDA, ROCm, espeak-ng or ffmpeg themselves.
- **No sudo/admin password:** everything goes in their home folder.
- Internet is only needed for the first downloads. After that it works offline.
- Uninstalling is one command, plus deleting `~/.local/share/tomecast`.

On first launch the app runs the hardware scan, lists what it needs to download (with sizes), asks once, and then does the following:
- **llama.cpp:** downloads the **official prebuilt build** from ggml-org's GitHub releases, pinned to one version we've tested (2026-10-28 or newer, for LFM3) and checked against its published checksum. It's kept in `~/.local/share/tomecast/runtime/`.
  - **Any graphics card (NVIDIA, AMD, Intel):** the Vulkan build, **35 MB**. It needs only the normal graphics driver.
  - **No graphics card:** the CPU build, **23 MB**.
  - The CUDA build (~170 MB + 440 MB runtime) isn't needed for models this small.
  - The app starts `llama-server` in the background only while it's needed, and stops it afterwards.
- **audio.cpp** (the voice engine): fetched the same way, from its official GitHub releases. CPU build 64 MB, or Vulkan build 116 MB for any graphics card.
- **ffmpeg** comes inside a Python package (`imageio-ffmpeg`). **eSpeak NG** is downloaded like the models, from the `espeakng-loader` wheel, pinned by SHA-256. So **nothing comes from the distro's package manager**.
- **No PyTorch anywhere:**
  - voices run on audio.cpp,
  - Heron runs on ONNX Runtime (a small Python package),
  - the text and OCR models run on llama.cpp.
- **Models** come only from official repos, in the sizes from section 10. Where a smaller file than the official one is needed, the app makes it locally with llama.cpp's own `llama-quantize` (seconds), instead of downloading files from unknown uploaders.
- **Self-check:** it runs a 2-second test of each piece. If a prebuilt engine won't start (for example on an older distro), it says so and falls back to its CPU build or the Python fallback.

**Where it works: the honest limits.** These were checked on 2026-11-05. audio.cpp's prebuilt Linux binaries (v0.9.4) need **glibc 2.43 or newer**. llama.cpp's main Linux downloads are built on Ubuntu 22.04. To check a system, run `ldd --version`.

| System | Result |
|---|---|
| Distros with glibc 2.43+: rolling ones like Arch / Manjaro / EndeavourOS / Omarchy and openSUSE Tumbleweed, and new releases that ship 2.43 | **Everything works** |
| Everything older, including Ubuntu 22.04 and 24.04, Debian 12 and 13, and current Fedora | **Not yet.** Tomecast installs, then explains that the voice engine needs glibc 2.43. Fix planned in [#18](https://github.com/Kingskid2026/tomecast/issues/18): build audio.cpp ourselves on an old base system (one build covers nearly every distro from 2019 on), with `kokoro-onnx` as a fallback |
| Alpine (musl) or NixOS | The prebuilt engines don't run as-is. Possible with extra setup, but not a first target |
| ARM Linux (Raspberry Pi, ARM laptops) | audio.cpp has no Linux ARM download yet, so this needs the `kokoro-onnx` fallback from #18. llama.cpp does have ARM builds |
| Very old CPUs (no AVX2) | Use audio.cpp's "portable" CPU build, which avoids newer CPU instructions |

**The one thing it can't install: graphics drivers.** Those need admin rights and differ per distro. The app uses whatever driver is already there. If Vulkan isn't working, it runs on the CPU and shows the single package to install for that distro (e.g. `vulkan-icd-loader` on Arch, `libvulkan1` on Ubuntu/Debian, `vulkan-loader` on Fedora).

### 10. Model sizes: quantized where it pays off
Rules of thumb from the research (2026-11-05):
- **8-bit is effectively lossless at any size**, as long as only the *weights* are quantized.
- **4-bit costs small models more than big ones**, because they have less spare capacity per weight. The exception is a model trained for 4-bit, like LFM3's QAD file.
- **The parts that make the final output are the most fragile:** a voice model's vocoder (the part that turns numbers into sound waves) and an OCR model's vision part. They stay at high precision.
- **Tiny models gain almost nothing from quantizing.** They aren't limited by memory speed, so they don't get faster, and their files barely shrink.
- **The app can keep a light and a full copy of a model** and switch between them automatically, using one at a time. That's only worth it where a fallback actually helps (PaddleOCR-VL).

| Model | File used | Size | Second copy? | Why |
|---|---|---|---|---|
| **Kokoro** 82M | **Q8_0** GGUF (audio.cpp). BF16 is not needed: measured, no artefact (see below) | 190 / 212 MB | No | Only 22 MB apart. **Avoid the common INT8 ONNX file.** It also quantizes activations, and was measured adding a steady tinny tone (4.8/9.6 kHz) and occasional garbled sentences, while running *slower* than full precision |
| **Ming-3.0-tts** 0.5B | **Q8_0 GGUF** (audio.cpp, official) | 1.74 GB (BF16: 3.0 GB) | No | In audio.cpp's tests, Q8 runs up to ~1.5× faster with ~37% less graphics memory than 16-bit. Below 8-bit isn't officially possible, because its 896-wide layers don't fit llama.cpp's 4–6-bit formats. A community 6.4-bit file (1.39 GB) exists, but from an uploader we can't vouch for |
| **Docling Heron** 43M | FP32 (ONNX) | 170 MB | No | Already under a second per page. An INT8 version that quantizes only the image-reading part (a similar detector lost ~0.5 accuracy points that way) gets tested, and is used only if it matches FP32 on our PDFs *and* is actually faster |
| **PaddleOCR-VL-1.6** | **Q4_K_M** language part + **BF16** vision part | ~0.3 + 0.88 GB | **Yes: Q8_0 (~0.5 GB)** | In one 4-bit test, 2 of 20 pages came out broken (0 of 20 at full precision), while intact pages were within ~1%. So pages are read at Q4, and any broken or garbled page is re-read with the Q8 copy. Both copies are made locally from one official download. The vision part breaks when quantized, so it stays full |
| **LFM3-1.7B** | **QAD-Q4_0** | 1.05 GB | No (Q8_0, 1.85 GB, only if testing calls for it) | Trained by Liquid to recover 4-bit losses. Its keep/drop job is forgiving, and it can never retype text. We measure its drift from full precision with llama.cpp's KL-divergence tool |
| MiniCPM5-2B (backup) | Q4_K_M | 1.56 GB | — | Only if LFM3 fails its tests |

#### Kokoro on audio.cpp: what was measured ([#3](https://github.com/Kingskid2026/tomecast/issues/3), 2026-11-05)
Real runs on the maintainer's machine, with audio.cpp 0.9.4 (CPU build) and the pinned Q8_0 file. The checks below were made from the signal and from eSpeak's phonemes; no one has listened to the files yet, so the by-ear judgment is still to do.

- **Made-up words.** *Galadriel whispered, "Namárië," as the boats drifted toward Lothlórien.* produced a normal 5.03 s file (peak 0.44 of full scale, average level 0.05, about 14 characters per second, ends in the usual short pad of silence, and it passes Tomecast's own `audio_problem` check), so unknown words are not dropped or cut. Kokoro never sees the spelling: eSpeak NG 1.52 turns every word into phonemes first, and for unknown words it uses its English spelling rules. Called directly, the same library gives:
  - *Galadriel* → `ɡælˈædɹiːəl` ("gal-AD-ree-ul": stress too late, and the first vowel is a short *a* instead of *uh*)
  - *Namárië* → `nˈæmɚɹˌɪɛ` ("NAM-er-ih-eh": the accents are ignored and the vowels are English ones, not *nah-MAH-ree-eh*)
  - *whispered* `wˈɪspɚd`, *drifted* `dɹˈɪftᵻd`, *Lothlórien* `ləθlˈɔːɹiən` ("luh-th-LOR-ee-un", close to *loth-LOR-ee-en*)

  So made-up names come out as readable, plausible English guesses, and some need a fix. The server doesn't log the phonemes (its log only has start-up lines), so these come from calling eSpeak's own library with the same data folder. `tomecast names` and the pronunciation lists are the answer for the ones that are off.
- **Exact phonetic spellings: not possible through audio.cpp 0.9.4's server or command line.**
  - Kokoro has a `phonemes` request option that skips eSpeak ("Kokoro-vocabulary phonemes to synthesize directly", one list entry per chunk) in the model spec and the source.
  - But it is a *list* option, and only the C library (`audiocpp_request_set_option_array`) can set a list. The server's `options` object and the command line's `--request-option` only fill string options, so `options.phonemes` is silently ignored. Tested: the audio with and without it is byte for byte identical, for both a valid and a deliberately broken phoneme string.
  - eSpeak's inline `[[phonemes]]` markup doesn't work either: its phoneme call reads the brackets out as letters ("g, at, el...").
  - So pronunciation lists use **respellings**, spoken through eSpeak like any other text. Findings that shaped the format: all-caps words are spelled letter by letter (so stress capitals are lowercased automatically), and a hyphen makes each piece its own stressed word (`her-my-oh-nee` → `hɜːmaɪˈoʊnˈiː`), so a respelling should be tried with `tomecast preview`, and `shi-vawn` for *Siobhan* works (`ʃivˈɔːn`). Supporting `/IPA/` later would need a way to pass a list (the C library through a small helper, or a future server version): not worth it yet.
- **Q8_0 vs BF16.** The BF16 file (212 MB, from the same Hugging Face commit as the pinned Q8_0, SHA-256 `0d2c2afa...`) was downloaded into a scratch folder only, and the same sentences voiced with both (same voice, same seed). For two passages (14 s and 18 s), a numpy spectrum of each (4096-point FFT averaged over the file) showed:
  - Average level in every band from 0 to 12 kHz within about 0.4 dB of each other.
  - No narrow tone at 4.8 kHz in either (within about 1.5 dB of the surrounding spectrum), and only a faint, equal bump at 9.6 kHz in both (+4.3 dB for Q8_0 and +5.1 dB for BF16 on the first passage, +2.8 and +2.5 dB on the second), so it belongs to Kokoro's vocoder and not to the quantization. In the quietest 20% of frames (the pauses), Q8_0 showed no tone that BF16 lacked; if anything BF16's pauses had the stronger 9.6 kHz bump on the first passage (+9.2 dB against +1.2 dB, from only 32 frames, so noisy).
  - The INT8 artefact comes from *activation* quantization in the ONNX file; Q8_0 GGUF quantizes only weights, which is why the rule of thumb at the top of this section holds here.

  **Decision: keep Q8_0.** Switching to BF16 would cost 22 MB for no measured gain. (On this CPU, one run each: Q8_0 2.9× and BF16 3.3× faster than real time, so BF16 is not slower either, but that is a single noisy run and the speed checks are tracked separately.)

Other ways it stays light:
- **One model in memory at a time.** The AI pass finishes and unloads before the voice starts.
- **llama.cpp memory:** the AI models' working memory (KV cache) is also 8-bit (`-ctk q8_0 -ctv q8_0`), and the context is capped at what a chapter needs (~16K tokens), not the full 256K.
- **Threads:** the scan picks the thread count, and uses fewer on a laptop running on battery.

Rough totals for a **CPU-only machine** (estimates, to be measured in build step 2):
- Voice only, English books: **~0.25 GB** (Kokoro + audio.cpp).
- Non-English books: the multilingual Kokoro file is ~0.93 GB, downloaded only when needed.
- With every AI add-on: **~3.2 GB**.
- The premium voice on a graphics card adds **~1.9 GB** (Ming + the Vulkan build).
- Peak memory: **~1.5–2 GB**, because only one model is loaded at a time.

### 11. Open source: the repo, quality, and updates

**Where it lives**
- GitHub: `github.com/Kingskid2026/tomecast` to start. The `tomecast` name on GitHub belongs to an inactive account from 2015. `tomecast-app` is free if we want an organization later, and GitHub redirects old links after a transfer.
- PyPI: `tomecast`.

**How updates reach people.** There are three kinds of update, all shipped from the repo:

1. **The app itself**
   - Version numbers follow "semantic versioning": `0.x` while it's young, `1.0.0` once it's stable. A bug fix bumps the last number (0.1.1), a new feature bumps the middle one (0.2.0).
   - **Every change goes in through a pull request**, and the automatic checks must pass.
   - **release-please** (a GitHub Action) keeps an always-open "Release 0.2.0" pull request, with the changelog written from the commit messages. **Merging it is the whole release:** it tags the version, builds the package, and publishes to PyPI.
   - Publishing uses **Trusted Publishing**, so no passwords or tokens are stored anywhere. Each build gets an attestation, a public proof that our CI built it from our code.
   - **On the user's side:** the app checks PyPI at most once a day. That's one anonymous request with no other tracking, and it can be turned off. When there's a new version it shows *"Tomecast 0.2.0 is available, what's new"*. `tomecast update` (or the button) runs `uv tool upgrade tomecast`. It never updates itself silently.
2. **Engines and models**
   - A `manifest.toml` inside the app lists the exact llama.cpp and audio.cpp versions, plus every model file's URL, size and SHA-256 checksum.
   - To move to a newer engine or model: change the manifest in a pull request, run the tests and the GPU checks on real hardware, then release.
   - On the user's next launch, the app downloads the new pieces and runs its self-check. It switches over only if the check passes, and **keeps the old version until then**, so it can roll back automatically.
   - **Never "latest":** a new llama.cpp or audio.cpp could break things, so users only ever get versions we've tested.
3. **The install script** is served straight from the repo's `main` branch (`raw.githubusercontent.com/Kingskid2026/tomecast/main/install.sh`), so it's always the reviewed version. It changes through pull requests like the code.

**What makes it a high-quality repo**

| Piece | What it is |
|---|---|
| **README** | What it does, a short demo GIF and an audio sample, the one-line install, the supported-distros table, quick start, FAQ, credits |
| **Licenses** | `LICENSE` for our code. `THIRD_PARTY.md` lists every model and engine the app downloads, with its license (Kokoro Apache 2.0, Ming MIT, LFM3 LFM Open License, …) |
| **Community files** | `CONTRIBUTING.md` (how to set up, test, and send a pull request), `CODE_OF_CONDUCT.md` (Contributor Covenant), `SECURITY.md` plus GitHub's private vulnerability reporting |
| **Issue templates** | The bug form asks for `tomecast doctor` output (distro, GPU, versions), so reports are useful from the first message |
| **Automatic checks on every pull request** | `ruff` (style and lint), `pyright` (type checks), `pytest` (readers and cleanup rules on public-domain sample books), a CPU-only end-to-end test (short EPUB → M4B with Kokoro), and **the install script run inside Ubuntu 22.04/24.04, Debian 12/13, Fedora and Arch containers**. That last one proves the "works on any distro" promise on every change |
| **Protected main branch** | Changes only land through pull requests with passing checks, ours included |
| **Dependabot** | Opens pull requests when Python packages or GitHub Actions have updates, and the checks show whether anything broke |
| **Docs** | `CHANGELOG.md` (generated), `docs/architecture.md` (this plan, cleaned up), `docs/models.md` (why each model and quantization), and how to add a new voice engine |
| **Real-hardware testing** | GitHub's free test machines have no graphics card, so each release has a short checklist: run `tomecast selftest --gpu` on the maintainer's NVIDIA laptop (RTX PRO 1000, 8 GB). Volunteers test AMD and old laptops on preview releases (`0.2.0rc1`) |
| **House rules** | Never commit copyrighted books (test files come only from Project Gutenberg and Standard Ebooks). Built-in voices only, with no cloning of real people's voices |

## Build order

0. **Set up the repo:** license, README skeleton, community files, the automatic checks, release-please, Trusted Publishing, and branch protection. Everything after this goes in through pull requests.
1. **Thin slice:** terminal command, EPUB in, Kokoro, M4B out with chapters. Proves the whole pipeline end to end.
2. **Check the engines and the newest models on real hardware.**
   - **audio.cpp:**
     - Ming Q8_0 on the RTX PRO 1000 (Vulkan and CUDA builds) and on CPU: measure speed and memory.
     - Kokoro: how it handles unknown words (the Elvish line), and whether it takes phonetic spellings. *Done: section 10.*
   - **LFM3-1.7B** on CPU only, with the GPU off and few threads, to stand in for an old laptop. Compare it against MiniCPM5-2B on the same tricky book sections, and measure QAD-Q4_0's drift from BF16 (llama.cpp KL divergence).
   - **Quantized vs full**, to confirm the section 10 picks hold up:
     - Kokoro Q8_0 vs BF16: by ear, plus a spectrum check for the 4.8/9.6 kHz tone. *Spectrum check done: section 10.*
     - Heron: backbone-INT8 vs FP32 on the test PDFs.
     - PaddleOCR-VL: Q4_K_M vs Q8_0, counting broken pages and typos.

   *If either struggles on normal hardware, we find out here, before building around it.*
3. Cleanup rules and the PDF reader. (The DOCX, FB2, TXT and HTML readers are done.)
4. Text prep: numbers, abbreviations, pronunciation lists, name finder. *Done for the terminal command; the window's Pronunciation tab is still to do ([#10](https://github.com/Kingskid2026/tomecast/issues/10)).*
5. Pause & resume and preview.
6. Hardware scan, the GPU switch, and speed estimates.
7. The desktop window.
8. The optional AI pass: Docling Heron for PDF layout, then LFM3-1.7B for judgment calls, then PaddleOCR-VL-1.6 for scanned pages.
9. **First public release (v0.1.0):** finished README, the install script on GitHub Pages, and a preview release for early testers first.

## Open questions

- Kindle MOBI/AZW3 support (DRM-free only) through Calibre's `ebook-convert`: later, or never.

## How we'll test it

- **Test books:**
  - a Project Gutenberg EPUB (plain markup)
  - a Standard Ebooks EPUB (rich tags, footnotes)
  - a text PDF with running headers, page numbers and footnotes
  - a scanned PDF from the Internet Archive
  - one each of DOCX, FB2 and TXT
  - a passage full of Tolkien-style names
- **Cleanup tests:** sample pages with known headers, page numbers and footnotes, checked against the exact expected spoken text.
- **AI pass:**
  - Run the same messy PDF with rules only and with Heron, and compare what's left.
  - Confirm LFM3's keep/drop answers never change a single word of the kept text (a diff check).
  - Time PaddleOCR-VL on a scanned chapter, on GPU and on CPU.
- **End to end:** convert a short book with each engine. Check chapters with `ffprobe` and play in VLC/mpv. Check MP3 tags and order.
- **Hardware:** on the maintainer's laptop, test the NVIDIA path and CPU-only (GPU switched off), and record speed and peak memory per engine. AMD and old-laptop paths need real machines from volunteer testers.
- **Resume:** kill the app mid-chapter, restart, and confirm no gaps or repeated sentences.
- **Listen check:** chapter starts, a footnote-heavy page, and the Elvish line: *Galadriel whispered, "Namárië," as the boats drifted toward Lothlórien.*
