"""Download engines and models, pinned by URL and SHA-256, into Tomecast's data folder, and
switch to new engine builds only once they have worked on this computer.

Nothing is ever used unless its SHA-256 matches the value pinned in :mod:`tomecast.manifest`.
Downloads are written to a temporary file and only moved into place once verified, and
archives are extracted with path checks so a malicious archive can't write outside its folder.

There are two kinds of asset:

* **Files** (the voice models) go straight into place once their checksum matches. They are
  data, the same on every computer, so a checksum is all the checking they need.
* **Archives** (engine builds, eSpeak NG) hold native programs that might not run on a given
  computer. Each build is extracted into its own folder, ``<path>/<first 12 hex digits of its
  SHA-256>/``, next to the build in use, and ``installed.json`` in the data folder records
  which build of each archive is in use. :func:`prepare` switches to a new build only after
  a self-check has passed on it; until then, and if the check fails, the build in use stays
  in use. Each archive keeps at most two builds: the one in use, plus either the new one (if
  it is waiting or failed its check) or the previous one (to go back to).

Builds that Tomecast 0.1.x extracted into ``engines/audiocpp-0.9.4-vulkan`` and the like are
adopted where they are (see :attr:`Asset.replaces`), so upgrading never deletes them before
the new build has passed its check.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tomecast import __version__

ProgressCallback = Callable[[int, int], None]  # (bytes done, total bytes)

_CHUNK_SIZE = 1024 * 1024
_MARKER = ".tomecast-sha256"
_STATE = "installed.json"
_LOCK = ".install.lock"
_TEMP_PREFIX = ".download-"
_BUILD_NAME_LENGTH = 12  # hex digits of the SHA-256 in a build folder's name

UPDATE_FAILED_NOTE = (
    "The voice engine update didn't work on this computer, so Tomecast kept using the "
    "previous version."
)
UPDATE_NOT_DOWNLOADED_NOTE = (
    "The voice engine update couldn't be downloaded, so Tomecast kept using the previous version."
)


class DownloadError(RuntimeError):
    """Raised when a download fails or doesn't match its pinned checksum."""


class DownloadDeclined(DownloadError):
    """The user said no to downloads that a run can't do without."""


@dataclass(frozen=True, slots=True)
class Asset:
    """A file Tomecast downloads: where from, what it must hash to, and where it goes."""

    name: str  # shown to the user, e.g. "Kokoro voice model"
    url: str
    sha256: str
    size: int  # bytes, for the "this will download N MB" prompt
    # Relative to the data folder. A file goes here; an archive's builds go in subfolders.
    path: str
    archive: Literal["tar.gz", "zip"] | None = None  # extract it if set
    # Archives only: folders older Tomecast versions extracted a build of it into. A build
    # found there is used (and kept until a newer one passes its check), not downloaded again.
    replaces: tuple[str, ...] = ()
    # Downloaded only when the user chooses it (a premium voice), never as part of a default
    # run. Nothing here treats it differently; it's for the code that picks what a run needs.
    opt_in: bool = False
    # What stays the same across versions ("audiocpp-vulkan"), naming its record in
    # installed.json. Empty: the path.
    id: str = ""

    @property
    def slot(self) -> str:
        """The name of this asset's record in ``installed.json``."""
        return self.id or self.path


# (the assets to download, True if every one is an update of a build in use) -> go ahead?
# Returning False skips the downloads when they are all updates; otherwise prepare() raises
# DownloadDeclined. A consent callback may also raise to stop.
Consent = Callable[[Sequence[Asset], bool], bool]
# (asset, its number, how many) -> how to report that asset's download, or None
DownloadReporter = Callable[[Asset, int, int], ProgressCallback | None]
# (the builds to check, the builds in use) -> None if the new ones work, else why not.
# Both map each archive's slot (Asset.slot) to a build folder; they differ only where a new
# build is waiting.
SelfCheck = Callable[[Mapping[str, Path], Mapping[str, Path]], str | None]


@dataclass(frozen=True, slots=True)
class Prepared:
    """What :func:`prepare` did."""

    notes: tuple[str, ...] = ()  # one plain sentence each, for the user
    switched: tuple[Asset, ...] = ()  # new builds that passed their check and are now in use
    held_back: tuple[Asset, ...] = ()  # pinned builds not in use: check failed, or no download


@dataclass(slots=True)
class _Slot:
    current: str | None = None  # the build in use, relative to the data folder
    previous: str | None = None  # the one before it, kept to go back to
    failed: dict[str, str] = field(default_factory=dict)  # {"sha256", "tomecast", "reason"}


def data_dir() -> Path:
    """Tomecast's data folder: ``$XDG_DATA_HOME/tomecast`` or ``~/.local/share/tomecast``."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "tomecast"


# --- Where things are ---------------------------------------------------------------------


def build_folder(asset: Asset, root: Path) -> Path:
    """Where the pinned build of an archive is extracted (whether or not it's in use)."""
    return root / asset.path / asset.sha256[:_BUILD_NAME_LENGTH]


def build_sha256(folder: Path) -> str | None:
    """The SHA-256 of the archive ``folder`` was extracted from, if it's a complete build."""
    try:
        text = (folder / _MARKER).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if len(text) == 64 and all(c in "0123456789abcdef" for c in text) else None


def build_in_use(asset: Asset, root: Path) -> Path | None:
    """The folder of the build of an archive that runs, or ``None`` if there is none."""
    current = _effective(asset, root, _load(root)).current
    return root / current if current else None


def local_path(asset: Asset, root: Path) -> Path:
    """Where ``asset`` is used from under ``root``: a file's path, or the folder of the
    archive's build in use (the pinned build's folder if none is installed yet)."""
    if not asset.archive:
        return root / asset.path
    return build_in_use(asset, root) or build_folder(asset, root)


def installed_sha256(asset: Asset, root: Path) -> str | None:
    """The SHA-256 of what is in use: the build in use, or the pinned file if it's there
    (files aren't hashed again here; see :func:`is_installed`)."""
    if not asset.archive:
        return asset.sha256 if (root / asset.path).is_file() else None
    folder = build_in_use(asset, root)
    return build_sha256(folder) if folder else None


def is_installed(asset: Asset, root: Path) -> bool:
    """True if the pinned version of ``asset`` is installed and in use (a file is hashed)."""
    if not asset.archive:
        target = root / asset.path
        return target.is_file() and _sha256_of(target) == asset.sha256
    return installed_sha256(asset, root) == asset.sha256


def is_usable(asset: Asset, root: Path) -> bool:
    """True if some version of ``asset`` can be used now: the pinned file, or any build of
    an archive (an older one while an update waits or after one failed its check)."""
    if not asset.archive:
        return is_installed(asset, root)
    return build_in_use(asset, root) is not None


def needs_download(asset: Asset, root: Path) -> bool:
    """True if :func:`prepare` would download ``asset``: its pinned version isn't on disk,
    and (for an archive) it hasn't already failed its check with this Tomecast version
    while an older build is there to use."""
    if not asset.archive:
        return not is_installed(asset, root)
    slot = _effective(asset, root, _load(root))
    if slot.current and (
        build_sha256(root / slot.current) == asset.sha256 or _failed_here(asset, slot)
    ):
        return False
    return build_sha256(build_folder(asset, root)) != asset.sha256


def waiting_check(asset: Asset, root: Path) -> bool:
    """True if the pinned build of an archive is on disk but not in use yet, because the
    build in use stays until the new one has passed a self-check."""
    if not asset.archive:
        return False
    slot = _effective(asset, root, _load(root))
    return (
        slot.current is not None
        and build_sha256(root / slot.current) != asset.sha256
        and build_sha256(build_folder(asset, root)) == asset.sha256
        and not _failed_here(asset, slot)
    )


def failure(asset: Asset, root: Path) -> str | None:
    """Why the pinned build of ``asset`` failed its self-check, if it did with this version."""
    slot = _effective(asset, root, _load(root))
    return slot.failed.get("reason", "") if _failed_here(asset, slot) else None


# --- Installing -----------------------------------------------------------------------------


def ensure(asset: Asset, root: Path, progress: ProgressCallback | None = None) -> Path:
    """Download ``asset`` unless its pinned version is on disk, and return where it is.

    A file goes straight into place once verified. An archive is extracted into its own
    build folder and becomes the build in use only if there is no other build (a first
    install); otherwise the build in use stays, and :func:`prepare` switches after the
    self-check. Nothing in use is ever removed or overwritten here.

    This doesn't lock the data folder: :func:`prepare` does. Use it alone only where one
    process installs (tests, CI).
    """
    if not asset.archive:
        target = root / asset.path
        if is_installed(asset, root):
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=_TEMP_PREFIX) as tmp:
            downloaded = Path(tmp) / "download"
            _download(asset, downloaded, progress)
            downloaded.replace(target)
        return target

    if is_installed(asset, root):
        return local_path(asset, root)
    target = build_folder(asset, root)
    if build_sha256(target) != asset.sha256:
        target.parent.parent.mkdir(parents=True, exist_ok=True)
        # The temporary folder sits beside the archive's folder, so a failed download leaves
        # nothing in it.
        with tempfile.TemporaryDirectory(dir=target.parent.parent, prefix=_TEMP_PREFIX) as tmp:
            downloaded = Path(tmp) / "download"
            _download(asset, downloaded, progress)
            extracted = Path(tmp) / "extracted"
            if asset.archive == "zip":
                _safe_extract_zip(downloaded, extracted)
            else:
                _safe_extract_tar(downloaded, extracted)
            (extracted / _MARKER).write_text(asset.sha256 + "\n")
            target.parent.mkdir(exist_ok=True)
            if target.exists() and not target.is_symlink():
                shutil.rmtree(target)  # an incomplete leftover: complete builds have a marker
            extracted.rename(target)
    if build_in_use(asset, root) is None:
        _promote(asset, root)  # a first install: nothing to protect
    return target


def prepare(
    assets: Sequence[Asset],
    root: Path,
    *,
    check: SelfCheck,
    consent: Consent = lambda missing, updates: True,
    on_download: DownloadReporter | None = None,
    on_check: Callable[[], None] | None = None,
) -> Prepared:
    """Make sure everything in ``assets`` is installed and that new engine builds work here.

    1. Builds from older Tomecast versions are adopted, and leftovers of interrupted
       downloads removed.
    2. What's missing is downloaded, if ``consent`` agrees. If it doesn't, or a download fails,
       but there's an older build of each missing archive to use, the run goes on with those.
    3. New archive builds that have an older build to fall back to get one ``check``, all
       together. If it passes they are switched to; if not, the older builds stay in use, the
       failure is recorded (so the same build isn't tried again by this Tomecast version),
       and a note says so. A first install has nothing to fall back to, so it isn't checked
       here: the run itself shows whether it works.
    4. Builds no longer needed are deleted: each archive keeps at most two.

    Holds a lock on the data folder throughout, so two Tomecast processes don't install at
    once.
    """
    notes: list[str] = []
    held_back: list[Asset] = []
    switched: tuple[Asset, ...] = ()
    with _locked(root):
        _tidy(assets, root)
        missing = [asset for asset in assets if needs_download(asset, root)]
        if missing:
            updates = all(_is_update(asset, root) for asset in missing)
            if not consent(missing, updates):
                if not updates:
                    raise DownloadDeclined("nothing was downloaded")
                held_back += missing
                missing = []
        for number, asset in enumerate(missing, start=1):
            progress = on_download(asset, number, len(missing)) if on_download else None
            try:
                ensure(asset, root, progress)
            except DownloadError:
                if not _is_update(asset, root):
                    raise
                notes.append(UPDATE_NOT_DOWNLOADED_NOTE)
                held_back.append(asset)
        waiting = [asset for asset in assets if waiting_check(asset, root)]
        if waiting:
            in_use = {
                asset.slot: folder
                for asset in assets
                if asset.archive and (folder := build_in_use(asset, root)) is not None
            }
            candidates = {**in_use, **{a.slot: build_folder(a, root) for a in waiting}}
            if on_check:
                on_check()
            problem = check(candidates, in_use)
            for asset in waiting:
                if problem is None:
                    _promote(asset, root)
                else:
                    _record_failure(asset, root, problem)
            if problem is None:
                switched = tuple(waiting)
            else:
                notes.append(UPDATE_FAILED_NOTE)
                held_back += waiting
        _clean_up(assets, root)
    return Prepared(tuple(dict.fromkeys(notes)), switched, tuple(held_back))


def forget_failures(root: Path) -> None:
    """Forget every failed self-check, so the next :func:`prepare` tries those builds again
    (``tomecast scan`` does this)."""
    with _locked(root):
        slots = _load(root)
        for slot in slots.values():
            slot.failed = {}
        _save(root, slots)


def _is_update(asset: Asset, root: Path) -> bool:
    """True if ``asset`` is an archive with an older build in use, so a run can go on
    without its download."""
    return asset.archive is not None and build_in_use(asset, root) is not None


def _failed_here(asset: Asset, slot: _Slot) -> bool:
    return slot.failed.get("sha256") == asset.sha256 and slot.failed.get("tomecast") == __version__


def _promote(asset: Asset, root: Path) -> None:
    """Make the pinned build of ``asset`` the one in use; the one it replaces is kept."""
    slots = _load(root)
    old = _effective(asset, root, slots)
    new = _relative(build_folder(asset, root), root)
    slots[asset.slot] = _Slot(current=new, previous=old.current if old.current != new else None)
    _save(root, slots)


def _record_failure(asset: Asset, root: Path, reason: str) -> None:
    slots = _load(root)
    slot = _effective(asset, root, slots)
    slot.failed = {"sha256": asset.sha256, "tomecast": __version__, "reason": reason[-2000:]}
    slots[asset.slot] = slot
    _save(root, slots)


def _tidy(assets: Sequence[Asset], root: Path) -> None:
    """Record the builds found on disk (from older versions) and remove what interrupted
    downloads left behind. Only called with the lock held."""
    slots = _load(root)
    for asset in assets:
        if asset.archive:
            slots[asset.slot] = _effective(asset, root, slots)
    _save(root, slots)
    parents = {(root / asset.path).parent for asset in assets}
    for parent in parents:
        for leftover in parent.glob(f"{_TEMP_PREFIX}*") if parent.is_dir() else ():
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover, ignore_errors=True)


def _clean_up(assets: Sequence[Asset], root: Path) -> None:
    """Delete builds that aren't needed. Each archive keeps the build in use, plus the pinned
    build if that isn't the one in use (waiting, or failed and kept for a retry), else the
    previous build. Only called with the lock held."""
    slots = _load(root)
    for asset in assets:
        if not asset.archive or asset.slot not in slots:
            continue
        slot = slots[asset.slot]
        if slot.current is None:
            continue
        keep = {slot.current}
        pinned = _relative(build_folder(asset, root), root)
        if pinned != slot.current and build_sha256(root / pinned) == asset.sha256:
            keep.add(pinned)
            slot.previous = None
        elif slot.previous and build_sha256(root / slot.previous):
            keep.add(slot.previous)
        else:
            slot.previous = None
        folder = root / asset.path
        others = [p for p in folder.iterdir()] if folder.is_dir() else []
        others += [root / legacy for legacy in asset.replaces]
        for other in others:
            if other.is_symlink() or not other.is_dir() or _relative(other, root) in keep:
                continue
            shutil.rmtree(other, ignore_errors=True)
    _save(root, slots)


# --- installed.json -----------------------------------------------------------------------


def _effective(asset: Asset, root: Path, slots: Mapping[str, _Slot]) -> _Slot:
    """``asset``'s record, with a build found on disk as the one in use when the record has
    none that is complete (a first run after upgrading from 0.1.x, or a lost state file)."""
    recorded = slots.get(asset.slot) or _Slot()
    if recorded.current and build_sha256(root / recorded.current):
        return _Slot(recorded.current, recorded.previous, dict(recorded.failed))
    for found in _builds_on_disk(asset, root):
        return _Slot(_relative(found, root), None, dict(recorded.failed))
    return _Slot(None, None, dict(recorded.failed))


def _builds_on_disk(asset: Asset, root: Path) -> Iterator[Path]:
    """Complete builds of ``asset``, the safest to adopt first: any other build in its
    folder (newest first), then those older Tomecast versions left, and the pinned one only
    last. Without a record, Tomecast can't tell whether the pinned build ever passed its
    check, so it is used only if it's the only build; otherwise it is checked like any
    update."""
    if not asset.archive:
        return
    pinned = build_folder(asset, root)
    folder = root / asset.path
    if folder.is_dir():
        others = [p for p in folder.iterdir() if p != pinned and not p.name.startswith(".")]
        others.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        yield from (p for p in others if p.is_dir() and build_sha256(p))
    yield from (root / legacy for legacy in asset.replaces if build_sha256(root / legacy))
    if build_sha256(pinned):
        yield pinned


def _load(root: Path) -> dict[str, _Slot]:
    try:
        payload = json.loads((root / _STATE).read_text())
    except (OSError, ValueError):
        return {}
    entries = payload.get("slots") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return {}
    slots = {}
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        failed = entry.get("failed")
        slots[str(name)] = _Slot(
            current=_safe_relative(entry.get("current")),
            previous=_safe_relative(entry.get("previous")),
            failed={str(k): str(v) for k, v in failed.items()} if isinstance(failed, dict) else {},
        )
    return slots


def _save(root: Path, slots: Mapping[str, _Slot]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": 1,
        "slots": {
            name: {
                "current": slot.current,
                "previous": slot.previous,
                **({"failed": slot.failed} if slot.failed else {}),
            }
            for name, slot in sorted(slots.items())
        },
    }
    with tempfile.NamedTemporaryFile(
        "w", dir=root, prefix=f".{_STATE}.", delete=False, encoding="utf-8"
    ) as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(f.name, root / _STATE)  # all at once: a reader sees the old or the new file


def _safe_relative(value: object) -> str | None:
    """A path from installed.json, if it stays inside the data folder."""
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / _LOCK).open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)  # waits for another Tomecast that is installing
        yield


# --- Downloading and extracting -----------------------------------------------------------


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
