# Spoken-text check (listen-back)

Status: researched and prototyped for [#11](https://github.com/Kingskid2026/tomecast/issues/11) (Ming). The decision logic is `src/tomecast/spoken_check.py`; it isn't wired into the conversion yet. This is point 5 of "Core guarantee" in [architecture.md](architecture.md).

## Why Ming needs it

Ming-3.0-tts sometimes skips a few words in the middle of a chunk: *"it occurred to her that she ought to have wondered"* comes out as *"it occurred to have wondered"*. On the earlier test runs it lost about 1–2% of words, in 3–15% of chunks, at every setting tried (guidance, seed, threads, build). The audio has a normal length, level and ending, so `audio_problem` passed every one of them. The only way to see a skip is to listen: transcribe the chunk and compare the words.

Kokoro doesn't need this: it loses nothing beyond the recogniser's own noise.

## Which recogniser

**Recommendation: Moonshine Streaming Tiny (Q8_0, 60 MB, MIT), run by the audio.cpp server Tomecast already ships.** It caught every skip the bigger models caught, runs at 30–40× real time on 4 CPU threads in 189 MB, and needs no new engine: only one more pinned model file. Moonshine Small and Parakeet are the fallbacks if Tiny's extra false alarms turn out to matter on more text (Small: 0 false alarms but 4× slower; Parakeet: 25 languages but 0.9 GB and 3 GB of memory).

All numbers measured on this laptop with **4 CPU threads, no graphics card**, on the same 43 minutes of Ming and Kokoro audio (see "How it was measured" for the load the machine was under). Speed is audio time ÷ wall time; "4 cores" is the same with four free cores.

| Option | Licence | Download | CPU speed (wall / 4 cores) | Peak memory | Skips caught / false alarms | Packaging |
|---|---|---|---|---|---|---|
| **audio.cpp + Moonshine Streaming Tiny Q8_0** | audio.cpp MIT; model MIT | **60 MB** | **30.6× / 40.9×** | **189 MB** | **86/86 · 2/290** | One GGUF from `audio-cpp/audio.cpp-gguf`, pinned by revision and SHA-256 like Kokoro and Ming. Same `audiocpp_server` (`moonshine_asr` family) |
| audio.cpp + Moonshine Streaming Small Q8_0 | MIT; MIT | 301 MB | 7.7× / 9.7× | 430 MB | 86/86 · 0/290 | Same |
| audio.cpp + Canary 180M Flash Q8_0 | MIT; CC-BY-4.0 | 249 MB | 4.1× / 5.9× | 467 MB | 83/86 · 0/290 | Same |
| audio.cpp + Parakeet TDT 0.6B v3 Q8_0 (what the Ming tests used) | MIT; CC-BY-4.0 | 916 MB | 4.1× / 5.9× | 2.98 GB | 86/86 · 1/290 | Same |
| whisper.cpp v1.9.5 + Whisper base.en Q8_0 | MIT; MIT | 82 MB model + a second engine | 1.4× / 2.6× | – | 3/3 · 0/57 (60 chunks) | A second C++ engine: no official Linux binaries, so Tomecast would have to build, pin and self-check it like audio.cpp (#27) |
| sherpa-onnx 2.0.1 + Parakeet TDT 0.6B v3 int8 | Apache-2.0; CC-BY-4.0 | 671 MB model + 4 MB wheel + `sherpa-onnx-core` (43 MB installed) | 4.4× (60 chunks) / – | 1.25 GB | 3/3 · 0/57 (60 chunks) | A PyPI dependency (wheels for Linux, Windows and macOS) plus four ONNX files pinned by SHA-256. Same model as audio.cpp's Parakeet at the same speed, in a second runtime |

Notes:

- **Whisper is slow on short chunks** because it always encodes a 30-second window: a 6-second chunk costs as much as 30 seconds of audio. Whisper small.en (264 MB) would be about 3× slower again; it didn't fit the time box.
- **Canary**'s decoder writes the most likely English, which is the wrong instinct here: it "heard" *sitting by her sister* where Ming had slurred it, and missed 3 skips.
- **Licences:** all allowed (no GPL or AGPL). CC-BY-4.0 (Parakeet, Canary) needs an attribution line in `THIRD_PARTY.md`. Niagara (also in audio.cpp, English) was ruled out: Applied Brain Research's own licence, not an open-source one.
- **Moonshine Streaming is English-only.** See "Wiring it in" for other languages.

## Comparing the words

`compare(expected_text, heard_text, prepare_heard=...)` in `spoken_check.py`:

1. **Normalise both sides the same way** (`spoken_words`): lowercase, accents removed (*Namárië* = *namarie*), apostrophes dropped (*Alice's* = *alices*, *o'clock* = *oclock*), and hyphens, dashes and slashes split words (*daisy-chain* = *daisy chain*).
2. **Numbers.** The chunk's text has already been through text prep, so its numbers are words. Recognisers write digits ("1865", "$5.50", "Dr."), so the transcript goes through **the same text prep** (`TextPrep.for_language(lang).body`) before comparing. "1865" then becomes "eighteen sixty-five", exactly what the voice was given.
3. **Align** with a minimum-edit (Levenshtein) table over words, not difflib's longest-block heuristic, so a skip that jumps between two copies of a repeated word is still lined up. Two words match when they're equal, or (both four letters or more, of similar length) at least 75% alike: *Lothlorien* and *Lothlorian*.
4. **Gaps.** Every run of the chunk's words that didn't match is a gap, with what was heard in its place. A gap's **lost words are net**: expected words minus heard words, and zero when the letters of the two sides match (*daisy chain* heard as *daisychain*, *down stairs* as *downstairs*). So a misheard name (one word heard as one other word) loses nothing, while *to her that she ought* heard as nothing loses five.
5. **Names and respellings.** A name the user respelled in the Names step is sent to the voice as the respelling (*noo zee-lund*) but written by the recogniser as the name (*New Zealand*). `compare(..., respellings=...)` takes the spoken sides of the book's pronunciation lists, and those words never count as lost.

Words the recogniser heard that weren't written (a repeated "oh dear", a filler) never count.

## What counts as a skip

`SkipRule` (defaults): a chunk has skipped words when

- one gap **lost 2 or more words**, or
- the chunk lost **3 or more words** in all (single words adding up), or
- **2 or more written words in a row were heard as something else entirely** (Ming slurring *sitting by her* into *Sidemiver's*: Moonshine heard three other words, so nothing is "lost" by count, but the words weren't said).

A single missing word is not enough on its own. Recognisers drop or merge short words now and then (*I shall* heard as *she'll*), and Kokoro, which doesn't skip, shows exactly that noise. Ming's real skips are bigger: of the 86 below, 5 lost 2 words, 14 lost 3, 40 lost 4, 24 lost 5, 2 lost 6 and 1 lost 11. So there is a clear gap between the two.

### Measured accuracy

376 chunks (352 Ming, 24 Kokoro); 86 Ming chunks skipped words (labels: see "How it was measured"). **None of the 86 was flagged by `audio_problem`.**

| Recogniser | Skips caught | False alarms on good chunks | Kokoro chunks flagged |
|---|---|---|---|
| **Moonshine Tiny** (audio.cpp) | **86 of 86** | **2 of 290** | 0 of 24 |
| Moonshine Small (audio.cpp) | 86 of 86 | 0 of 290 | 0 of 24 |
| Parakeet TDT 0.6B v3 (audio.cpp, CPU; the GPU run of 10 Nov gives the same) | 86 of 86 | 1 of 290 | 0 of 24 |
| Canary 180M Flash (audio.cpp) | 83 of 86 | 0 of 290 | 0 of 24 |
| Whisper base.en (whisper.cpp), 60 fresh chunks only | 3 of 3 | 0 of 57 | – |
| Parakeet int8 (sherpa-onnx), 60 fresh chunks only | 3 of 3 | 0 of 57 | – |

Moonshine Tiny isn't one of the recognisers that made the labels, so its row is an independent check; Parakeet's, Canary's and Moonshine Small's agree with labels they helped make. A false alarm costs one regenerate of that chunk; a miss would ship missing words, so the rule leans toward flagging.

The disagreements, read by hand:

- *"…a rabbit with either a waistcoat-pocket…"* (UK_Charlotte, seed 7): every recogniser heard a non-word for *waistcoat* (Parakeet *exicopat*, Moonshine Small *hexacult*, Moonshine Tiny *exocult*, Canary *rexicut*). Ming mispronounced the word but didn't skip anything; Parakeet and Moonshine Tiny flag it because *a* got absorbed into the non-word. A false alarm by the labels, but a regenerate wouldn't hurt.
- *"…falling down a very deep well"* (US_James, guidance 4): Moonshine Tiny alone heard *out of* for *down a*. A real false alarm; it costs one regenerate.
- *"…sitting by her sister…"* (US_James, three settings): Ming slurred it (Parakeet *Sidemiver's*, Moonshine *Sid and Myver's*), and Canary, whose decoder writes the most likely English, heard *sitting by her sister* or lost only single words. Canary's three misses.

On the same chunks the old scoring script (`score.py`, net dropped words ≥ 2 from difflib) found the same 86, plus one false alarm on Kokoro (*I shall* heard as *she'll* and *down stairs* as *downstairs*), which the letter check here removes.

## Regenerating, and failing loudly

What the earlier Ming runs say about fixing a skip (same 4 voices, *Alice* chapter I):

| Retry | Effect on chunks that skipped |
|---|---|
| Same text, new seed | Fixes about **1 in 4**. Of 21 voice-and-chunk pairs that skipped with seed 42 or 7, 12 skipped with both. |
| guidance 3 or 4, sigma 0 | **No help**: 12–13 of 20 failing chunks still skipped, against 12 of 20 at the defaults. |
| Same text cut into pieces of ≤ 80 characters | Fixes **11 of 12**: 1 of 20 chunks still lost words (3 words in all), against 12 of 20 (49 words). |

Skips depend on the text, so a new seed rarely helps, while the same words cut into smaller pieces almost always come out whole. (Splitting *every* chunk up front isn't a cure: on fresh text, 80-character pieces still lost 24 words against 29. Only re-cutting the chunk that failed works.)

`speak_checked(chunk_id, text, synthesize, listener, seed=…, plan=RetryPlan())`:

1. Voice the chunk and check it. Most chunks stop here.
2. **One new seed** (`RetryPlan.reseeds = 1`) for the whole chunk, which keeps the sentence's rhythm in one piece.
3. **Split at 80 characters** with `chunking.split_long` (the same splitter as chunking: commas and semicolons first, and no text can be lost), and check each piece on its own, each with its own seed and one reseed.
4. A piece that still fails is **split again at 40 characters**.
5. If a piece at the smallest size still loses words, raise **`WordsSkippedError`**, naming the chunk ID, the missing words and the text. The conversion stops, as with any other gap: no audio missing words is ever kept.

Each attempt gets a new seed (never one already tried). The pieces come back in reading order, for the existing join step to concatenate with its short gaps, and the chunk's file gets its final name only after all of it has passed, so resume keeps working.

Seed first or split first is a maintainer's call. A reseed fixes 1 in 4 and keeps the chunk whole; going straight to 80 characters fixes 11 in 12 and saves about one synthesis per failing chunk (about 6% of Ming's time on a book where 10% of chunks fail). The default keeps the reseed; `RetryPlan(reseeds=0)` is the other choice.

**Open decision: what to do after the last retry.** Stopping is what AGENTS.md asks for, and is the default. Two softer options for later, both visible to the user, never silent: voice that one chunk with Kokoro and say so on the done screen, or show the chunk in the window to accept or edit. A chunk the recogniser can never hear right (an invented word heard as nothing) would otherwise stop a whole book.

## What it costs per book

For a 3-hour book (10,800 s of audio). "Wall" is what this loaded laptop gave; "4 cores" is the same engine with four free cores (see "How it was measured").

**Listening, on the CPU, 4 threads:**

| Recogniser | Wall | 4 cores |
|---|---|---|
| **Moonshine Tiny** (recommended) | **5.9 min** (30.6×) | **4.4 min** (40.9×) |
| Moonshine Small | 23 min (7.7×) | 19 min (9.7×) |
| Parakeet TDT 0.6B v3 | 44 min (4.1×) | 31 min (5.9×) |

**Regenerating:** in these runs 5% (fresh text at the defaults) to 18% (the drop scan) of Ming's chunks skipped. At about 10%, the retry plan voices about 18% more audio in all (one reseed for every failing chunk, then pieces for the three in four the reseed doesn't fix). With Ming at 5.4× real time on the NVIDIA card (34 min for this book), that is **about 6 more minutes**, plus listening to the retries.

**With Ming on the graphics card,** listening runs beside it on the CPU: chunk *n* is checked while chunk *n + 1* is voiced. Ming makes about 4–6.5 s of audio per second, and Moonshine Tiny listens at 30–40× on 4 threads, so it keeps up while using **about 15% of four cores**, adding **no wall time** beyond the last chunk. That matters because Ming's own loop is host-bound and slows down when the CPU is busy (about 15% on CUDA under heavy load, in the 10 November tests). Parakeet on the CPU couldn't keep up with Ming at 5–6.5× and would compete with it for the CPU.

**Listening on a graphics card:** not measured today (the cards were in use). Earlier runs scored Ming chunks with Parakeet on the AMD 860M through Vulkan at about 39× real time, model load included (1,518 s of audio in 39 s), which would be about 4.6 minutes for this book, beside Ming on the NVIDIA card. On the same card as Ming, Parakeet's Q8 file is 0.9 GB next to Ming's 1.45 GB. Moonshine Tiny on the CPU is fast enough that a card isn't needed for it.

**On a computer without a usable graphics card,** Ming itself is the bottleneck by far; Moonshine Tiny adds 4–6 minutes per 3-hour book and 189 MB of memory, against Parakeet's 30–45 minutes and 3 GB.

## Wiring it in with Ming (#11)

Not done in this pull request. What it takes:

- **Pin the model** in `manifest.py`: `Moonshine-Streaming-GGUF/moonshine-streaming-tiny-q8_0.gguf` from `audio-cpp/audio.cpp-gguf` at revision `73ce4162f6cce370dd533c6782b772135c1b2a8e`, 60,409,088 bytes, SHA-256 `0f795f14924dbfe40d2ee0e220382c9b23cd9e89064c6ffb97e236d81f1d7020`. Add it to `THIRD_PARTY.md` (MIT, Moonshine AI). Download it with Ming, not with Kokoro.
- **No new engine.** Tomecast's own audio.cpp build doesn't narrow `AUDIOCPP_MODEL_SET`, whose default is `full`, so `moonshine_asr` (and `parakeet_tdt`) are already compiled in. Start a second `audiocpp_server` on the **CPU** backend with the ASR model (189 MB), and keep Ming's server on the graphics card. A small `AudioCppTranscriber` posts `{"model": "asr", "audio": "<path>", "language": "en"}` to `/v1/audio/transcriptions` and returns `text`; it satisfies `Transcriber`, so the engine can change without touching the rest.
- **In the voice loop,** for voices that need it (Ming; Kokoro doesn't), call `speak_checked` with a `Listener(transcriber, prepare_heard=TextPrep.for_language(lang).body, respellings=<spoken sides of the book's lists>)`. Join split pieces with the existing join step. Transcribe chunk *n* while Ming voices chunk *n + 1*, so the check stays off the critical path.
- **The self-check** (`VoiceCheck` for Ming) should also run the recogniser once, so a broken ASR model is caught before a book starts and not by a book that "skips" every chunk.
- **Language.** Moonshine Streaming is English-only. Ming speaks English and Chinese; for other languages, Parakeet TDT 0.6B v3 (25 European languages, same server) is the fallback, at its larger size.
- **`WordsSkippedError`** stops the conversion with its message, on the command line and in the window, like the other "no usable audio" errors.

## How it was measured

- **Audio:** 376 chunks already on disk from the Ming tests of 8 and 10 November (`ming-test/runs/`): the drop scan (24 chunks × 4 Ming voices × seeds 42 and 7, plus the same 24 chunks from Kokoro af_heart as a control), 60 fresh chunks at the default settings, and the 100 chunks of the mitigation test. 43 minutes of audio, from *Alice's Adventures in Wonderland* (public domain), each chunk at most 200 characters after Tomecast's text prep.
- **Engines:** audio.cpp v0.9.4's official CPU build (the families above), whisper.cpp v1.9.5 built here for the CPU, sherpa-onnx 2.0.1 from PyPI. All with **4 threads, CPU only**: no graphics card was used.
- **Load:** other jobs kept this laptop (16 threads, 31 GB) at a load average of 20–50, with memory full and swapping. So "wall" speeds are pessimistic, and "4 cores" (audio time ÷ the engine's own CPU time ÷ 4) is what the engine does with four free cores.
- **Labels:** nobody has listened to these 376 chunks. A chunk counts as a real skip when most of the four strongest recognisers (Parakeet, Canary, Moonshine Small, Whisper base) flag it under the rule above, and every disagreement was read by hand (transcripts below). Each engine is then scored against those labels, so the numbers say how well an engine agrees with the consensus, not with a human ear.
- **Not committed:** the scripts and transcripts live outside the repo with the other Ming test files; no audio or model files are in the repo.
