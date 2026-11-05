"""Tests for MP3 output (one file per chapter), run against the real bundled ffmpeg."""

from __future__ import annotations

import array
import math
import re
import subprocess
from pathlib import Path

import pytest
from test_packaging import (
    FFMPEG,
    INFO,
    RATE,
    chapter,
    decode,
    extract_cover,
    probe,
    read_metadata,
    three_chapters,
    tiny_jpeg,
    tiny_png,
    write_tone,
)

from tomecast import packaging
from tomecast.book import Cover
from tomecast.packaging import (
    AudiobookInfo,
    ChapterAudio,
    PackagedBook,
    PackagingError,
    mp3_file_name,
    write_mp3s,
)

# One MP3 frame at 24 kHz is 576 samples (MPEG-2 Layer III); allow one frame per file.
FRAME_SLACK = 576 / RATE


def make(tmp_path: Path, chapters: list[ChapterAudio], **options: object) -> PackagedBook:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    options.setdefault("workdir", tmp_path / "work")
    return write_mp3s(chapters, options.pop("info", INFO), out / "The Test Book", **options)  # type: ignore[arg-type]


def files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix == ".mp3")


@pytest.fixture(scope="module")
def book(tmp_path_factory: pytest.TempPathFactory) -> tuple[PackagedBook, list[ChapterAudio]]:
    tmp_path = tmp_path_factory.mktemp("mp3book")
    chapters = three_chapters(tmp_path / "segments")
    return make(tmp_path, chapters), chapters


def test_one_file_per_chapter_named_and_ordered(
    book: tuple[PackagedBook, list[ChapterAudio]],
) -> None:
    packaged, _ = book
    assert packaged.output.name == "The Test Book"
    assert [p.name for p in files(packaged.output)] == [
        "01 - Prologue.mp3",
        "02 - The Road.mp3",
        "03 - Home.mp3",
    ]
    assert [c.title for c in packaged.chapters] == ["Prologue", "The Road", "Home"]
    assert sorted(p.name for p in packaged.output.parent.iterdir()) == ["The Test Book"]


def test_numbers_are_zero_padded_to_the_chapter_count() -> None:
    assert mp3_file_name(3, 12, "Home") == "03 - Home.mp3"
    assert mp3_file_name(3, 9, "Home") == "03 - Home.mp3"
    assert mp3_file_name(7, 120, "Home") == "007 - Home.mp3"
    assert mp3_file_name(1, 12, "   ") == "01.mp3"


def test_file_names_are_safe() -> None:
    assert mp3_file_name(1, 1, "AC/DC: \x00Live\n") == "01 - AC-DC - Live.mp3"
    assert mp3_file_name(1, 1, "..hidden") == "01 - hidden.mp3"
    long = mp3_file_name(1, 1, "é" * 200)
    assert len(long.encode()) < 120
    assert long.endswith("é.mp3")
    assert "/" not in mp3_file_name(1, 1, "a/b/c")


def test_tags_include_track_n_of_total(book: tuple[PackagedBook, list[ChapterAudio]]) -> None:
    packaged, _ = book
    for number, path in enumerate(files(packaged.output), start=1):
        tags = read_metadata(path).tags
        assert tags["album"] == "The Test Book"
        assert tags["artist"] == "Ada Writer, Bo Coauthor"
        assert tags["album_artist"] == "Ada Writer, Bo Coauthor"
        assert tags["track"] == f"{number}/3"
        assert tags["genre"] == "Audiobook"
        assert tags["composer"] == "Kokoro (af_heart)"
        assert tags["title"] == packaged.chapters[number - 1].title
        assert path.read_bytes()[:4] == b"ID3\x03"  # ID3v2.3


def test_audio_is_mono_mp3_at_the_voice_rate(
    book: tuple[PackagedBook, list[ChapterAudio]],
) -> None:
    packaged, _ = book
    for path in files(packaged.output):
        info = probe(path)
        assert len(info.audio) == 1
        assert info.audio[0].startswith("mp3")
        assert "24000 Hz, mono" in info.audio[0]
        assert "64 kb/s" in info.audio[0]
        assert info.video == []


def test_durations_match_the_segments_and_gaps(
    book: tuple[PackagedBook, list[ChapterAudio]],
) -> None:
    packaged, _ = book
    expected = [1.6, 1.3, 2.25]
    for path, chapter_info, seconds in zip(
        files(packaged.output), packaged.chapters, expected, strict=True
    ):
        assert chapter_info.end_seconds - chapter_info.start_seconds == pytest.approx(seconds)
        assert probe(path).duration == pytest.approx(seconds, abs=FRAME_SLACK + 0.01)
    assert packaged.chapters[-1].end_seconds == pytest.approx(sum(expected))


def test_every_chunk_is_in_exactly_one_file(tmp_path: Path) -> None:
    """Each chunk is a tone of its own pitch; find every one at its place in its chapter's file,
    and in no other file, and check the file lengths add up to every chunk plus its gap."""
    chapters: list[ChapterAudio] = []
    placed: list[tuple[int, float, float, float, str]] = []  # file, start, end, pitch, name
    pitch = 300.0
    expected_frames = 0
    for index, title in enumerate(["One", "Two", "Three", "Four"]):
        parts = [(0.30 + 0.05 * i, 0.10 + 0.05 * (i % 2)) for i in range(index + 2)]
        segments = []
        position = 0.0
        for i, (seconds, gap) in enumerate(parts):
            wav = write_tone(tmp_path / "segments" / title / f"{i}.wav", seconds, frequency=pitch)
            segments.append(wav)
            placed.append((index, position, position + seconds, pitch, f"{title}/{i}"))
            expected_frames += round(seconds * RATE) + round(gap * RATE)
            pitch += 150
            position += seconds + gap
        chapters.append(ChapterAudio(title, tuple(segments), tuple(g for _, g in parts)))

    packaged = make(tmp_path, chapters)
    outputs = files(packaged.output)
    assert len(outputs) == 4
    decoded = [decode(p) for p in outputs]

    # One MP3 frame of slack per file, as the encoder pads its last frame.
    assert sum(len(d) for d in decoded) == pytest.approx(expected_frames, abs=len(outputs) * 1152)

    for file_index, start, end, frequency, name in placed:
        lo, hi = round((start + 0.03) * RATE), round((end - 0.03) * RATE)
        assert _goertzel_power(decoded[file_index][lo:hi], frequency) > 0.5, (
            f"chunk {name} is missing from file {file_index + 1}"
        )
        for other, samples in enumerate(decoded):
            if other != file_index:
                assert _goertzel_power(samples, frequency) < 0.02, (
                    f"chunk {name} also shows up in file {other + 1}"
                )


def _goertzel_power(samples: array.array[int], frequency: float) -> float:
    """Share of the signal's energy at ``frequency`` (0..1), by the Goertzel algorithm."""
    if not samples:
        return 0.0
    coefficient = 2 * math.cos(2 * math.pi * frequency / RATE)
    s1 = s2 = 0.0
    energy = 0.0
    for sample in samples:
        s0 = sample + coefficient * s1 - s2
        s2, s1 = s1, s0
        energy += sample * sample
    power = s1 * s1 + s2 * s2 - coefficient * s1 * s2
    return 0.0 if energy == 0 else min(1.0, 2 * power / (energy * len(samples)))


def test_tempo_halves_the_durations(tmp_path: Path) -> None:
    chapters = three_chapters(tmp_path / "segments")
    packaged = make(tmp_path, chapters, tempo=2.0)
    for path, seconds in zip(files(packaged.output), [1.6, 1.3, 2.25], strict=True):
        assert probe(path).duration == pytest.approx(seconds / 2, abs=0.06)
    assert packaged.chapters[-1].end_seconds == pytest.approx(5.15 / 2)


@pytest.mark.parametrize("tempo", [0.4, 2.5, math.nan])
def test_tempo_outside_the_range_is_refused(tmp_path: Path, tempo: float) -> None:
    with pytest.raises(PackagingError, match="tempo"):
        make(tmp_path, [chapter(tmp_path, "A", [(0.3, 0.1)])], tempo=tempo)


@pytest.mark.parametrize("media_type", ["image/jpeg", "image/png"])
def test_cover_is_embedded_as_front_cover_in_every_file(tmp_path: Path, media_type: str) -> None:
    data = tiny_jpeg(tmp_path) if media_type == "image/jpeg" else tiny_png()
    info = AudiobookInfo("The Test Book", ("Ada",), "Kokoro", None, Cover(data, media_type))
    packaged = make(tmp_path, three_chapters(tmp_path / "segments"), info=info)
    for path in files(packaged.output):
        stream = probe(path)
        assert len(stream.audio) == 1
        assert len(stream.video) == 1
        assert extract_cover(path) == data
        listing = subprocess.run(
            [FFMPEG, "-hide_banner", "-nostdin", "-i", str(path)],
            capture_output=True,
            check=False,
        ).stderr.decode()
        assert re.search(r"comment\s*:\s*Cover \(front\)", listing)
        assert "attached pic" in listing


def test_no_cover_means_no_picture(book: tuple[PackagedBook, list[ChapterAudio]]) -> None:
    packaged, _ = book
    assert all(probe(p).video == [] for p in files(packaged.output))


def test_empty_titles_become_chapter_n(tmp_path: Path) -> None:
    chapters = [chapter(tmp_path, "", [(0.3, 0.1)]), chapter(tmp_path, "Two", [(0.3, 0.1)])]
    packaged = make(tmp_path, chapters)
    assert [p.name for p in files(packaged.output)] == ["01 - Chapter 1.mp3", "02 - Two.mp3"]
    assert read_metadata(files(packaged.output)[0]).tags["title"] == "Chapter 1"


def test_special_characters_in_titles_and_tags(tmp_path: Path) -> None:
    title = "A=B; C#D \\ E/F"
    info = AudiobookInfo("Dune: Messiah", ("Frank",), "Kokoro", None, None)
    packaged = make(tmp_path, [chapter(tmp_path, title, [(0.3, 0.1)])], info=info)
    (path,) = files(packaged.output)
    assert "/" not in path.name
    tags = read_metadata(path).tags
    assert tags["title"] == title
    assert tags["album"] == "Dune: Messiah"


def test_never_overwrite_numbers_the_folder(tmp_path: Path) -> None:
    chapters = [chapter(tmp_path, "A", [(0.3, 0.1)])]
    out = tmp_path / "out"
    out.mkdir()
    (out / "The Test Book").mkdir()
    (out / "The Test Book" / "mine.txt").write_text("keep")
    (out / "The Test Book (2)").write_text("a file, not a folder")

    packaged = write_mp3s(
        chapters, INFO, out / "The Test Book", workdir=tmp_path / "w", overwrite=False
    )

    assert packaged.output == out / "The Test Book (3)"
    assert [p.name for p in files(packaged.output)] == ["01 - A.mp3"]
    assert (out / "The Test Book" / "mine.txt").read_text() == "keep"
    assert [p.name for p in (out / "The Test Book").iterdir()] == ["mine.txt"]
    assert sorted(p.name for p in out.iterdir()) == [
        "The Test Book",
        "The Test Book (2)",
        "The Test Book (3)",
    ]


def test_never_overwrite_survives_a_folder_appearing_while_encoding(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    target = out / "The Test Book"
    real = packaging._encode

    def encode_then_someone_saves(*args: object, **kwargs: object) -> None:
        real(*args, **kwargs)  # type: ignore[arg-type]
        target.mkdir(exist_ok=True)
        (target / "theirs.mp3").write_bytes(b"theirs")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(packaging, "_encode", encode_then_someone_saves)
        packaged = write_mp3s(
            [chapter(tmp_path, "A", [(0.3, 0.1)])],
            INFO,
            target,
            workdir=tmp_path / "w",
            overwrite=False,
        )

    assert packaged.output == out / "The Test Book (2)"
    assert (target / "theirs.mp3").read_bytes() == b"theirs"
    assert [p.name for p in target.iterdir()] == ["theirs.mp3"]


def test_overwrite_replaces_old_chapters_but_keeps_other_files(tmp_path: Path) -> None:
    out = tmp_path / "out"
    target = out / "The Test Book"
    target.mkdir(parents=True)
    (target / "01 - Old.mp3").write_bytes(b"old")
    (target / "02 - Gone.mp3").write_bytes(b"old")
    (target / "notes.txt").write_text("mine")

    packaged = write_mp3s(
        [chapter(tmp_path, "New", [(0.3, 0.1)])], INFO, target, workdir=tmp_path / "w"
    )

    assert packaged.output == target
    assert sorted(p.name for p in target.iterdir()) == ["01 - New.mp3", "notes.txt"]
    assert probe(target / "01 - New.mp3").duration > 0.3


def test_overwrite_refuses_a_file_in_the_way(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "The Test Book").write_text("a file")
    with pytest.raises(PackagingError, match="isn't a folder"):
        write_mp3s(
            [chapter(tmp_path, "A", [(0.3, 0.1)])],
            INFO,
            out / "The Test Book",
            workdir=tmp_path / "w",
        )
    assert (out / "The Test Book").read_text() == "a file"


def test_failure_leaves_nothing_behind(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    chapters = three_chapters(tmp_path / "segments")
    truncated = chapters[2].segments[1]
    truncated.write_bytes(truncated.read_bytes()[:-4000])  # the header says more than is there
    with pytest.raises(PackagingError):
        write_mp3s(chapters, INFO, out / "The Test Book", workdir=tmp_path / "w")
    assert list(out.iterdir()) == []


def test_missing_ffmpeg_is_reported(tmp_path: Path) -> None:
    with pytest.raises(PackagingError, match="ffmpeg"):
        make(tmp_path, [chapter(tmp_path, "A", [(0.3, 0.1)])], ffmpeg=str(tmp_path / "nope"))
    assert list((tmp_path / "out").iterdir()) == []


def test_bad_input_is_rejected_before_encoding(tmp_path: Path) -> None:
    with pytest.raises(PackagingError, match="no chapters"):
        make(tmp_path, [])
    assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Chapter 1: The Night", "Chapter 1 - The Night"),
        ('What? "No" <yes> a|b\\c*', "What 'No' yes a-b-c"),
        ("CON", "CON_"),
        ("nul.txt", "nul.txt_"),
        ("  ..Hidden. ", "Hidden"),
        ("Tab\there/slash", "Tab here-slash"),
    ],
)
def test_names_work_on_usb_sticks(title: str, expected: str) -> None:
    # Car stereos read FAT32/exFAT sticks, which refuse \ : * ? " < > | and device names.
    from tomecast.packaging import portable_name

    assert portable_name(title) == expected
    assert not set(portable_name(title)) & set('\\:*?"<>|/')
