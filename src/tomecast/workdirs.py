"""Work folders for resumable conversions, shared by the terminal command and the window.

Each conversion keeps its chunk audio in ``<data folder>/cache/convert-<key>/``. The key is a
hash of the book file, the voice, the speed, the seed, and the engine build in use (CPU or
graphics card, and which build of it) and model versions, so the same book with the same
settings resumes in the same folder, and a resumed book never mixes two voices, models or
engine builds.
Folders left unused for :data:`STALE_WORK_DAYS` are removed.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import shutil
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

from tomecast.downloads import Asset, build_sha256, installed_sha256
from tomecast.engines import ming
from tomecast.engines.audiocpp import AUDIOCPP_VERSION, DEFAULT_SEED, required_assets
from tomecast.voices import work_key

# Unfinished conversions are kept so they can be resumed; after this long they're removed.
STALE_WORK_DAYS = 14


class WorkFolderBusy(RuntimeError):
    """Raised when another run is already converting the same book with the same settings."""


def work_folder(
    root: Path,
    book_path: Path,
    voice: str,
    speed: float,
    *,
    use_gpu: bool = False,
    builds: Mapping[str, Path] | None = None,
) -> Path:
    """The folder for this book's chunk audio: the same book and settings resume in it.

    ``builds`` are the engine builds the run uses (``Prepared.builds``, which the engine is
    also given), so the key names exactly what voices the book. Without them, the builds in
    use now. Also removes work folders that haven't been used for :data:`STALE_WORK_DAYS`.
    """
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - STALE_WORK_DAYS * 86400
    for old in cache.glob("convert-*"):
        if old.is_dir() and old.stat().st_mtime < cutoff:
            shutil.rmtree(old, ignore_errors=True)
    digest = hashlib.sha256()
    with book_path.open("rb") as f:
        while block := f.read(1024 * 1024):
            digest.update(block)
    # Anything that changes how the audio sounds goes in the key, so a resumed book never
    # mixes two voices, speeds or model versions. The builds are the ones in use, which are
    # the pinned ones unless an update didn't work on this computer (or isn't installed yet).
    # A Kokoro voice is keyed by its name alone (as before); a Ming voice also by its flow
    # steps, and each by the files its own engine needs (Ming's model, no eSpeak).
    settings = [work_key(voice), str(speed), f"seed={DEFAULT_SEED}", f"audiocpp-{AUDIOCPP_VERSION}"]
    assets = (
        ming.required_assets(use_gpu) if ming.is_ming_voice(voice) else required_assets(use_gpu)
    )
    settings += [_sha256_in_use(a, root, builds or {}) for a in assets]
    digest.update("|".join(settings).encode())
    folder = cache / f"convert-{digest.hexdigest()[:16]}"
    folder.mkdir(exist_ok=True)
    folder.touch()  # counts as recently used
    return folder


def _sha256_in_use(asset: Asset, root: Path, builds: Mapping[str, Path]) -> str:
    if asset.slot in builds:
        return build_sha256(builds[asset.slot]) or asset.sha256
    return installed_sha256(asset, root) or asset.sha256


@contextlib.contextmanager
def exclusive(folder: Path) -> Iterator[None]:
    """Hold a lock on ``folder`` so two runs can't convert the same book at once.

    Raises:
        WorkFolderBusy: if another run (in this process or another one) holds the lock.
    """
    with (folder / ".lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkFolderBusy(
                "this book is already being converted with the same settings "
                "(in another window or terminal?)"
            ) from None
        yield
