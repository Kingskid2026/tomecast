"""Download engines and models, pinned by URL and SHA-256, into Tomecast's data folder.

Nothing is ever used unless its SHA-256 matches the value pinned in the code, downloads are
written to a temporary file and only moved into place once verified, and archives are
extracted with path checks so a malicious archive can't write outside its folder.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tomecast import __version__

ProgressCallback = Callable[[int, int], None]  # (bytes done, total bytes)

_CHUNK_SIZE = 1024 * 1024
_MARKER = ".tomecast-sha256"


class DownloadError(RuntimeError):
    """Raised when a download fails or doesn't match its pinned checksum."""


@dataclass(frozen=True, slots=True)
class Asset:
    """A file Tomecast downloads: where from, what it must hash to, and where it goes."""

    name: str  # shown to the user, e.g. "Kokoro voice model"
    url: str
    sha256: str
    size: int  # bytes, for the "this will download N MB" prompt
    path: str  # relative to the data folder; for archives, the folder to extract into
    archive: Literal["tar.gz", "zip"] | None = None  # extract into ``path`` if set


def data_dir() -> Path:
    """Tomecast's data folder: ``$XDG_DATA_HOME/tomecast`` or ``~/.local/share/tomecast``."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "tomecast"


def local_path(asset: Asset, root: Path) -> Path:
    """Where ``asset`` lives once installed under ``root``."""
    return root / asset.path


def is_installed(asset: Asset, root: Path) -> bool:
    """True if ``asset`` is installed and was verified against its pinned checksum."""
    target = local_path(asset, root)
    if asset.archive:
        marker = target / _MARKER
        return marker.is_file() and marker.read_text().strip() == asset.sha256
    return target.is_file() and _sha256_of(target) == asset.sha256


def ensure(asset: Asset, root: Path, progress: ProgressCallback | None = None) -> Path:
    """Install ``asset`` under ``root`` if it isn't already, and return its local path."""
    target = local_path(asset, root)
    if is_installed(asset, root):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".download-") as tmp:
        downloaded = Path(tmp) / "download"
        _download(asset, downloaded, progress)
        if asset.archive:
            extracted = Path(tmp) / "extracted"
            if asset.archive == "zip":
                _safe_extract_zip(downloaded, extracted)
            else:
                _safe_extract_tar(downloaded, extracted)
            (extracted / _MARKER).write_text(asset.sha256 + "\n")
            if target.exists():
                shutil.rmtree(target)
            extracted.rename(target)
        else:
            downloaded.replace(target)
    return target


def _download(asset: Asset, destination: Path, progress: ProgressCallback | None) -> None:
    if not asset.url.startswith(("https://", "file://")):
        raise DownloadError(f"refusing to download {asset.name} over an insecure URL")
    request = urllib.request.Request(asset.url, headers={"User-Agent": f"tomecast/{__version__}"})
    digest = hashlib.sha256()
    done = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as f:
            while block := response.read(_CHUNK_SIZE):
                f.write(block)
                digest.update(block)
                done += len(block)
                if progress:
                    progress(done, asset.size)
    except OSError as error:
        raise DownloadError(f"couldn't download {asset.name} from {asset.url}: {error}") from error
    if digest.hexdigest() != asset.sha256:
        raise DownloadError(
            f"{asset.name} failed its checksum check (expected {asset.sha256}, "
            f"got {digest.hexdigest()}). The file was not used."
        )


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    """Extract a .tar.gz, refusing any member that would land outside ``destination``."""
    if not hasattr(tarfile, "data_filter"):  # added in Python 3.11.4
        raise DownloadError("extracting downloads safely needs Python 3.11.4 or newer")
    destination.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            # The "data" filter rejects absolute paths, "..", links that escape the folder,
            # and device files.
            tar.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as error:
        raise DownloadError(f"couldn't extract {archive.name}: {error}") from error


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    """Extract a .zip (or wheel), refusing any member that would land outside ``destination``."""
    destination.mkdir()
    root = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if not (destination / name).resolve().is_relative_to(root):
                    raise DownloadError(f"couldn't extract {archive.name}: unsafe path {name!r}")
            zf.extractall(destination)
    except (zipfile.BadZipFile, OSError) as error:
        raise DownloadError(f"couldn't extract {archive.name}: {error}") from error


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(_CHUNK_SIZE):
            digest.update(block)
    return digest.hexdigest()
