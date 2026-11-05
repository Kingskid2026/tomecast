import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from tomecast.downloads import Asset, DownloadError, data_dir, ensure, is_installed


def _file_asset(source: Path, *, path: str = "models/thing.bin", archive: bool = False) -> Asset:
    data = source.read_bytes()
    return Asset(
        name="test asset",
        url=source.as_uri(),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        path=path,
        archive=archive,
    )


def _make_tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_downloads_verifies_and_installs_a_file(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"model weights")
    asset = _file_asset(source)
    root = tmp_path / "data"
    progress: list[tuple[int, int]] = []

    installed = ensure(asset, root, progress=lambda done, total: progress.append((done, total)))

    assert installed == root / "models" / "thing.bin"
    assert installed.read_bytes() == b"model weights"
    assert is_installed(asset, root)
    assert progress[-1] == (13, 13)


def test_a_checksum_mismatch_is_rejected_and_nothing_is_installed(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"tampered")
    asset = Asset("test asset", source.as_uri(), "0" * 64, 8, "models/thing.bin")
    root = tmp_path / "data"

    with pytest.raises(DownloadError, match="checksum"):
        ensure(asset, root)

    assert not (root / "models" / "thing.bin").exists()
    assert list((root / "models").iterdir()) == []  # no temp files left behind


def test_insecure_urls_are_refused(tmp_path: Path) -> None:
    asset = Asset("test asset", "http://example.com/x", "0" * 64, 1, "x")
    with pytest.raises(DownloadError, match="insecure"):
        ensure(asset, tmp_path)


def test_installed_files_are_not_downloaded_again(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"weights")
    asset = _file_asset(source)
    root = tmp_path / "data"
    ensure(asset, root)
    source.unlink()  # a second download would fail now

    assert ensure(asset, root).read_bytes() == b"weights"


def test_archives_are_extracted_into_their_folder(tmp_path: Path) -> None:
    archive = tmp_path / "engine.tar.gz"
    _make_tar(archive, {"engine/bin/tts": b"binary", "engine/README": b"hi"})
    asset = _file_asset(archive, path="engines/audiocpp", archive=True)
    root = tmp_path / "data"

    folder = ensure(asset, root)

    assert (folder / "engine" / "bin" / "tts").read_bytes() == b"binary"
    assert is_installed(asset, root)


def test_archives_that_escape_their_folder_are_refused(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    _make_tar(archive, {"../../escaped": b"gotcha"})
    asset = _file_asset(archive, path="engines/evil", archive=True)
    root = tmp_path / "data"

    with pytest.raises(DownloadError, match="extract"):
        ensure(asset, root)

    assert not (tmp_path / "escaped").exists()
    assert not (root / "engines" / "evil").exists()


def test_data_dir_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert data_dir() == tmp_path / "tomecast"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert data_dir() == Path.home() / ".local" / "share" / "tomecast"
