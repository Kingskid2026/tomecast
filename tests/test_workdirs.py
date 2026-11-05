import os
import time
from pathlib import Path

import pytest

from tomecast.workdirs import STALE_WORK_DAYS, WorkFolderBusy, exclusive, work_folder


def _book(tmp_path: Path, name: str = "book.epub", data: bytes = b"book bytes") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_the_same_book_and_settings_share_a_folder(tmp_path: Path) -> None:
    root = tmp_path / "data"
    book = _book(tmp_path)

    first = work_folder(root, book, "af_heart", 1.0)
    second = work_folder(root, book, "af_heart", 1.0)

    assert first == second
    assert first.is_dir()
    assert first.parent == root / "cache"
    assert first.name.startswith("convert-")


def test_a_copy_of_the_book_elsewhere_resumes_in_the_same_folder(tmp_path: Path) -> None:
    root = tmp_path / "data"
    original = _book(tmp_path)
    (tmp_path / "copy").mkdir()
    copy = _book(tmp_path / "copy", "renamed.epub")

    assert work_folder(root, original, "af_heart", 1.0) == work_folder(root, copy, "af_heart", 1.0)


@pytest.mark.parametrize(
    ("voice", "speed", "data"),
    [("bm_george", 1.0, b"book bytes"), ("af_heart", 1.1, b"book bytes"),
     ("af_heart", 1.0, b"another book")],
)  # fmt: skip
def test_anything_that_changes_the_audio_changes_the_folder(
    tmp_path: Path, voice: str, speed: float, data: bytes
) -> None:
    root = tmp_path / "data"
    baseline = work_folder(root, _book(tmp_path), "af_heart", 1.0)
    other = work_folder(root, _book(tmp_path, "other.epub", data), voice, speed)

    assert other != baseline


def test_stale_folders_are_removed_and_used_ones_kept(tmp_path: Path) -> None:
    root = tmp_path / "data"
    stale = root / "cache" / "convert-0000000000000000"
    (stale / "chunks").mkdir(parents=True)
    recent = root / "cache" / "convert-1111111111111111"
    recent.mkdir()
    unrelated = root / "cache" / "audiocpp-tmp"
    unrelated.mkdir()
    old = time.time() - (STALE_WORK_DAYS + 1) * 86400
    for folder in (stale, unrelated):
        os.utime(folder, (old, old))

    folder = work_folder(root, _book(tmp_path), "af_heart", 1.0)

    assert not stale.exists()
    assert recent.is_dir()
    assert unrelated.is_dir()  # only conversion folders are cleaned up
    assert folder.stat().st_mtime > old


def test_using_a_folder_again_marks_it_recently_used(tmp_path: Path) -> None:
    root = tmp_path / "data"
    book = _book(tmp_path)
    folder = work_folder(root, book, "af_heart", 1.0)
    old = time.time() - (STALE_WORK_DAYS - 1) * 86400
    os.utime(folder, (old, old))

    assert work_folder(root, book, "af_heart", 1.0) == folder
    assert folder.stat().st_mtime > old + 86400


def test_only_one_run_holds_a_folder_at_a_time(tmp_path: Path) -> None:
    folder = work_folder(tmp_path / "data", _book(tmp_path), "af_heart", 1.0)

    with (
        exclusive(folder),
        pytest.raises(WorkFolderBusy, match="already being converted"),
        exclusive(folder),
    ):
        pass  # pragma: no cover

    with exclusive(folder):  # free again once the first run is done
        pass
