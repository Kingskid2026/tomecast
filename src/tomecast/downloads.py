"""Download engines and models, pinned by URL and SHA-256, into Tomecast's data folder, and
switch to new engine builds only once they have worked on this computer.

Nothing is ever used unless its SHA-256 matches the value pinned in :mod:`tomecast.manifest`.
Downloads are written to a temporary file and only moved into place once verified, and
archives are extracted with path checks so a malicious archive can't write outside its folder.

There are two kinds of asset:

* **Files** (the voice models) go straight into place once their checksum matches. They are
  data, the same on every computer, so a checksum is all the checking they need. A file is
  hashed once; after that a marker beside it (its size and modification time) says it was
  verified, so a 1.7 GB model isn't read in full on every run.
* **Archives** (engine builds, eSpeak NG) hold native programs that might not run on a given
  computer. Each build is extracted into its own folder, ``<path>/<first 12 hex digits of its
  SHA-256>/``, next to the build in use, and ``installed.json`` in the data folder records
  which build of each archive is in use. :func:`prepare` switches to a new build only after
  the self-checks of the voices that use it have passed; until then, and if one fails, the
  build in use stays in use. A switched-to build that then fails a real run is rolled back
  (:func:`roll_back`) until it has finished one (:func:`mark_proven`). Each archive usually
  keeps two builds: the one in use, plus either the new one (if it is waiting or failed its
  check) or the previous one (to go back to); see :func:`_clean_up` for when it keeps three.

Builds that Tomecast 0.1.x extracted into ``engines/audiocpp-0.9.4-vulkan`` and the like are
adopted where they are (see :attr:`Asset.replaces`), so upgrading never deletes them before
the new build has passed its check.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tomecast import __version__

ProgressCallback = Callable[[int, int], None]  # (bytes done, total bytes)
IsCancelled = Callable[[], bool]  # True once the user has stopped the run

_CHUNK_SIZE = 1024 * 1024  # bytes hashed at a time
_DOWNLOAD_READ = 64 * 1024  # the most bytes taken from a download at a time
_MARKER = ".tomecast-sha256"
_INSTALLED_BY = ".tomecast-installed-by"  # in a build folder: the Tomecast that installed it
_VERIFIED = ".tomecast-verified"  # beside a file: its SHA-256, size and mtime once hashed
_STATE = "installed.json"
_LOCK = ".install.lock"
_TEMP_PREFIX = ".download-"
_BUILD_NAME_LENGTH = 12  # hex digits of the SHA-256 in a build folder's name
_LOCK_POLL = 0.2  # seconds between tries for the install lock
# A build that failed its check (or a real run) is tried again after this long, by a newer
# Tomecast, or after `tomecast scan`: a busy or misbehaving computer shouldn't block an update
# for good.
FAILURE_DAYS = 3

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


class Cancelled(Exception):
    """The user stopped the run (Cancel, or closing the window) while Tomecast was waiting
    for, installing or checking an engine build. Nothing is recorded as failed."""


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


@dataclass(frozen=True, slots=True)
class Inconclusive:
    """A self-check that couldn't tell whether the new build works, because what failed
    wasn't the build (the graphics card failed with the old build too). Nothing is switched
    or recorded, and the next run checks again."""

    reason: str


# None: the new builds work; a str: why they don't; Inconclusive: no verdict.
CheckResult = str | Inconclusive | None
# (the builds to check, the builds in use) -> a CheckResult. Both map each archive's slot
# (Asset.slot) to a build folder; they differ only where a new build is waiting.
SelfCheck = Callable[[Mapping[str, Path], Mapping[str, Path]], CheckResult]


@dataclass(frozen=True, slots=True)
class VoiceCheck:
    """How one voice checks the builds it runs on: Kokoro voices a sentence with the engine
    and eSpeak, Ming (#11) will voice one with the engine alone. A new build is judged only by
    the voices that use it, and switched to only if all of them pass."""

    name: str  # "Kokoro"
    uses: frozenset[str]  # the slots of the archives this voice runs on
    run: SelfCheck


# (the assets to download, True if every one is an update of a build in use) -> go ahead?
# Returning False skips the downloads when they are all updates; otherwise prepare() raises
# DownloadDeclined. A consent callback may also raise to stop.
Consent = Callable[[Sequence[Asset], bool], bool]
# (asset, its number, how many) -> how to report that asset's download, or None
DownloadReporter = Callable[[Asset, int, int], ProgressCallback | None]


@dataclass(frozen=True, slots=True)
class Prepared:
    """What :func:`prepare` did, and which builds the run is to use."""

    notes: tuple[str, ...] = ()  # one plain sentence each, for the user
    switched: tuple[Asset, ...] = ()  # new builds that passed their check and are now in use
    held_back: tuple[Asset, ...] = ()  # pinned builds not in use: check failed, or no download
    # Each archive's slot -> the build folder this run uses. Give it to the engine and the
    # work folder, so a whole run (crash restarts included) uses exactly these builds.
    builds: Mapping[str, Path] = field(default_factory=dict)


@dataclass(slots=True)
class _Slot:
    current: str | None = None  # the build in use, relative to the data folder
    previous: str | None = None  # the one before it, kept to go back to
    # The Tomecast version that made `current` the build in use ("" for builds 0.1.x left).
    # An older Tomecast never switches away from a newer one's choice.
    chosen_by: str = ""
    # `current` has finished a real run (or was there before this one was switched to), so a
    # later failure isn't blamed on it and nothing is rolled back.
    proven: bool = True
    # SHA-256 -> {"tomecast", "reason", "at" (seconds since 1970), "during" ("check"/"run")}
    failed: dict[str, dict[str, str]] = field(default_factory=dict)


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


def run_builds(assets: Sequence[Asset], root: Path) -> dict[str, Path]:
    """The build of each archive in ``assets`` that a run of this Tomecast uses: the build in
    use, except where a newer Tomecast chose it, when this one's own pinned build is used
    (without switching to it), so each version runs what it was tested with. Only if that's
    the build the newer one switched away from (``previous``), so it has worked here: one
    that ``tomecast scan`` downloaded but that hasn't passed its check is never run."""
    slots = _load(root)
    builds = {}
    for asset in assets:
        if not asset.archive:
            continue
        slot = _effective(asset, root, slots)
        own = build_folder(asset, root)
        if (
            _chosen_by_newer(slot)
            and slot.previous == _relative(own, root)
            and build_sha256(own) == asset.sha256
            and not _failed_here(asset.sha256, slot)
        ):
            builds[asset.slot] = own
        elif slot.current:
            builds[asset.slot] = root / slot.current
    return builds


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
    """True if the pinned version of ``asset`` is installed and in use. A file is hashed the
    first time, and after that only if its size or modification time changes."""
    if not asset.archive:
        return _file_verified(asset, root, hash_if_unsure=True)
    return installed_sha256(asset, root) == asset.sha256


def is_usable(asset: Asset, root: Path) -> bool:
    """True if some version of ``asset`` can be used now: the pinned file, or any build of
    an archive (an older one while an update waits or after one failed its check)."""
    if not asset.archive:
        return is_installed(asset, root)
    return build_in_use(asset, root) is not None


def needs_download(
    asset: Asset, root: Path, *, verify: bool = True, take_over: bool = False
) -> bool:
    """True if :func:`prepare` would download ``asset``: its pinned version isn't on disk,
    and (for an archive) it hasn't already failed its check while an older build is there to
    use, and no newer Tomecast has chosen the build in use.

    ``verify=False`` never hashes: a file of the pinned size that hasn't been verified yet
    counts as there (``prepare`` verifies it before use). For the window's GUI thread.
    ``take_over`` ignores a newer Tomecast's choice (see :func:`prepare`).
    """
    if not asset.archive:
        return not _file_verified(asset, root, hash_if_unsure=verify)
    slot = _effective(asset, root, _load(root))
    if slot.current and (
        build_sha256(root / slot.current) == asset.sha256
        or _failed_here(asset.sha256, slot)
        or (_chosen_by_newer(slot) and not take_over)
    ):
        return False
    return build_sha256(build_folder(asset, root)) != asset.sha256


def waiting_check(asset: Asset, root: Path, *, take_over: bool = False) -> bool:
    """True if the pinned build of an archive is on disk but not in use yet, because the
    build in use stays until the new one has passed a self-check. ``take_over`` ignores a
    newer Tomecast's choice (see :func:`prepare`)."""
    if not asset.archive:
        return False
    slot = _effective(asset, root, _load(root))
    return (
        slot.current is not None
        and build_sha256(root / slot.current) != asset.sha256
        and build_sha256(build_folder(asset, root)) == asset.sha256
        and not _failed_here(asset.sha256, slot)
        and not (_chosen_by_newer(slot) and not take_over)
    )


def failure(asset: Asset, root: Path) -> str | None:
    """Why the pinned build of ``asset`` failed (its check, or a real run), if it did
    recently with this version of Tomecast."""
    slot = _effective(asset, root, _load(root))
    if not _failed_here(asset.sha256, slot):
        return None
    return slot.failed[asset.sha256].get("reason", "")


def mark_verified(asset: Asset, root: Path) -> None:
    """Record that the file of ``asset`` matches its SHA-256 as it is now (size and
    modification time), so it isn't hashed again. Only call this once that's known."""
    target = root / asset.path
    stat = target.stat()
    marker = {"sha256": asset.sha256, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    _write_atomic(_verified_marker(target), json.dumps(marker) + "\n")


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
            _download(asset, downloaded, progress)  # verified while downloading
            downloaded.replace(target)
        mark_verified(asset, root)
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
            (extracted / _INSTALLED_BY).write_text(__version__ + "\n")
            (extracted / _MARKER).write_text(asset.sha256 + "\n")
            target.parent.mkdir(exist_ok=True)
            if target.exists() and not target.is_symlink():
                _discard(target)  # an incomplete leftover: complete builds have a marker
            extracted.rename(target)
    # A first install (nothing else on disk: the pinned build is adopted only when it's the
    # only one) has nothing to protect, so it's recorded as in use straight away.
    if _effective(asset, root, _load(root)).current in (None, _relative(target, root)):
        _promote(asset, root, proven=True)
    return target


def prepare(
    assets: Sequence[Asset],
    root: Path,
    *,
    check: SelfCheck | Sequence[VoiceCheck],
    consent: Consent = lambda missing, updates: True,
    on_download: DownloadReporter | None = None,
    on_check: Callable[[], None] | None = None,
    cancelled: IsCancelled | None = None,
    take_over: bool = False,
    on_wait: Callable[[], None] | None = None,
) -> Prepared:
    """Make sure everything in ``assets`` is installed and that new engine builds work here.

    1. Builds from older Tomecast versions are adopted, and leftovers of interrupted
       downloads removed.
    2. What's missing is downloaded, if ``consent`` agrees. If it doesn't, or a download fails,
       but there's an older build of each missing archive to use, the run goes on with those.
       ``consent`` is asked with the lock let go (an unanswered question must never keep
       another Tomecast waiting), so what's missing is worked out again afterwards: what
       another Tomecast installed meanwhile isn't downloaded twice, and anything newly
       missing is asked about too.
    3. New archive builds that have an older build to fall back to are checked by each voice
       that uses them (``check``: one :class:`SelfCheck` for every archive, or one
       :class:`VoiceCheck` per voice), all swapped in together. A failed check is tried once
       more. If every check passes they are switched to, together; if one fails, the older
       builds stay in use, each failed build is recorded (so it isn't tried again for
       :data:`FAILURE_DAYS` days, unless Tomecast is updated or ``tomecast scan`` runs), and a
       note says so. A check with no verdict changes nothing, and the next run checks again.
       A first install has nothing to fall back to, so it isn't checked here: the run itself
       shows whether it works (and a switched-to build that fails its first run is rolled
       back, see :func:`roll_back`).
    4. Builds no longer needed are deleted: each archive usually keeps two (see
       :func:`_clean_up`).

    Holds a lock on the data folder throughout (except while ``consent`` is asked), so two
    Tomecast processes don't install at once. Each time another Tomecast holds it,
    ``on_wait`` is called once, to say so. While waiting for it, and during the checks,
    ``cancelled`` is asked whether the user has stopped; if so this raises
    :class:`Cancelled` and records nothing.

    Normally an older Tomecast leaves a build a newer one chose alone (``chosen_by``), so two
    installed versions don't take turns switching. ``take_over`` (``tomecast scan``, run by
    the user) lets this one download and check its own build anyway, and switch to it if it
    passes, which then records this version as the one that chose it. So a Tomecast that was
    downgraded gets back onto the build it was tested with, and no choice is ever wiped.
    """
    checks = _as_checks(check, assets)
    agreed: set[Asset] = set()
    declined: set[Asset] = set()  # updates only: a run can go on without them
    while True:
        with _locked(root, cancelled, on_wait=on_wait):
            _tidy(assets, root)
            missing = [a for a in assets if needs_download(a, root, take_over=take_over)]
            ask = [
                asset
                for asset in missing
                if asset not in agreed and (asset not in declined or not _is_update(asset, root))
            ]
            if not ask:
                return _install(
                    assets, root, checks,
                    downloads=[asset for asset in missing if asset in agreed],
                    declined=[asset for asset in missing if asset in declined],
                    on_download=on_download, on_check=on_check, cancelled=cancelled,
                    take_over=take_over,
                )  # fmt: skip
            updates = all(_is_update(asset, root) for asset in ask)
        if consent(ask, updates):
            agreed.update(ask)
            declined.difference_update(ask)
        elif updates:
            declined.update(ask)
        else:
            raise DownloadDeclined("nothing was downloaded")


def _install(
    assets: Sequence[Asset],
    root: Path,
    checks: Sequence[VoiceCheck],
    *,
    downloads: Sequence[Asset],
    declined: Sequence[Asset],
    on_download: DownloadReporter | None,
    on_check: Callable[[], None] | None,
    cancelled: IsCancelled | None,
    take_over: bool,
) -> Prepared:
    """Steps 2 to 4 of :func:`prepare`, once the user has agreed to ``downloads`` and
    declined the updates in ``declined``. Only called with the lock held."""
    notes: list[str] = []
    held_back = list(declined)
    switched: list[Asset] = []
    for number, asset in enumerate(downloads, start=1):
        progress = on_download(asset, number, len(downloads)) if on_download else None
        try:
            ensure(asset, root, progress)
        except DownloadError:
            if not _is_update(asset, root):
                raise
            notes.append(UPDATE_NOT_DOWNLOADED_NOTE)
            held_back.append(asset)
    waiting = [a for a in assets if waiting_check(a, root, take_over=take_over)]
    if waiting:
        if on_check:
            on_check()
        verdicts = _judge(waiting, assets, root, checks, cancelled)
        if all(result is None for result in verdicts.values()):
            for asset in waiting:
                _promote(asset, root, proven=False)
            switched += waiting
        else:
            # They were checked together, so they switch together. A build that failed is
            # recorded; the others wait, and the next run checks them with the builds in use
            # instead of the failed one.
            for asset, result in verdicts.items():
                if isinstance(result, str):
                    _record_failure(asset.slot, asset.sha256, root, result, during="check")
                    notes.append(UPDATE_FAILED_NOTE)
            held_back += waiting
    _clean_up(assets, root)
    builds = run_builds(assets, root)
    return Prepared(tuple(dict.fromkeys(notes)), tuple(switched), tuple(held_back), builds)


# How long recording a finished or failed run waits for another Tomecast that is installing:
# the user has their book (or their error) already, and the next run can record it instead.
PROVE_WAIT = 2.0
ROLL_BACK_WAIT = 10.0


def roll_back(
    root: Path,
    used: Mapping[str, Path],
    reason: str,
    cancelled: IsCancelled | None = None,
    *,
    wait: float = ROLL_BACK_WAIT,
) -> bool:
    """A run with the builds ``used`` (``Prepared.builds``) failed: go back to the previous
    build of each archive that this Tomecast (or an older one) switched to and that hasn't
    finished a real run yet, and record the failure, so the next run uses the build that
    worked and the failed one isn't picked again (for :data:`FAILURE_DAYS` days). True if
    anything was rolled back. A build a newer Tomecast chose is left alone, as everywhere.

    A build that has finished a run before isn't blamed: the problem is more likely the book,
    the computer or the card. If another Tomecast holds the install lock for more than
    ``wait`` seconds, nothing is rolled back this time (False): the build is still unproven,
    so its next failure rolls it back."""
    rolled = False
    with contextlib.suppress(_Busy), _locked(root, cancelled, wait):
        slots = _load(root)
        for name, folder in used.items():
            slot = slots.get(name)
            if (
                slot is None
                or slot.current is None
                or folder != root / slot.current
                or slot.proven
                or _chosen_by_newer(slot)
                or not slot.previous
                or not build_sha256(root / slot.previous)
            ):
                continue
            bad = build_sha256(root / slot.current)
            if bad is not None:
                slot.failed[bad] = _failure_record(reason, during="run")
            slot.current, slot.previous, slot.proven = slot.previous, None, True
            rolled = True
        if rolled:
            _save(root, slots)
    return rolled


def mark_proven(
    root: Path,
    used: Mapping[str, Path],
    cancelled: IsCancelled | None = None,
    *,
    wait: float = PROVE_WAIT,
) -> bool:
    """A run with the builds ``used`` (``Prepared.builds``) finished: they have proven
    themselves on this computer, so a later failure won't roll them back. Only call this for a
    run that used them as intended (not one that fell back from the graphics card to the
    CPU). Call it after telling the user the book is done: if another Tomecast holds the
    install lock for more than ``wait`` seconds this gives up quietly (False), and the next
    finished book proves them instead."""
    proven = False
    with contextlib.suppress(_Busy), _locked(root, cancelled, wait):
        slots = _load(root)
        changed = False
        for name, folder in used.items():
            slot = slots.get(name)
            if slot and slot.current and folder == root / slot.current and not slot.proven:
                slot.proven = changed = True
        if changed:
            _save(root, slots)
        proven = True
    return proven


def forget_failures(
    root: Path,
    cancelled: IsCancelled | None = None,
    *,
    on_wait: Callable[[], None] | None = None,
) -> None:
    """Forget every failed self-check and run, so the next :func:`prepare` tries those builds
    again (``tomecast scan`` does this). Which Tomecast chose each build in use is kept: a
    scan takes a build back through a checked switch (``prepare(take_over=True)``), never by
    wiping that record. ``on_wait`` is as in :func:`prepare`."""
    with _locked(root, cancelled, on_wait=on_wait):
        slots = _load(root)
        for slot in slots.values():
            slot.failed = {}
        _save(root, slots)


def _as_checks(
    check: SelfCheck | Sequence[VoiceCheck], assets: Sequence[Asset]
) -> list[VoiceCheck]:
    if callable(check):
        return [VoiceCheck("all", frozenset(a.slot for a in assets if a.archive), check)]
    return list(check)


def _judge(
    waiting: Sequence[Asset],
    assets: Sequence[Asset],
    root: Path,
    checks: Sequence[VoiceCheck],
    cancelled: IsCancelled | None,
) -> dict[Asset, CheckResult]:
    """Run each voice's check that uses one of the waiting builds (all of them swapped in
    together) and decide each build: a failure beats no verdict, which beats a pass. A build
    no voice uses gets no verdict: nothing is ever switched to without a check."""
    in_use = {
        asset.slot: folder
        for asset in assets
        if asset.archive and (folder := build_in_use(asset, root)) is not None
    }
    candidates = {**in_use, **{a.slot: build_folder(a, root) for a in waiting}}
    verdicts: dict[Asset, CheckResult] = {
        asset: None
        if any(asset.slot in voice.uses for voice in checks)
        else Inconclusive("no voice that uses it could check it")
        for asset in waiting
    }
    for voice in checks:
        judged = [asset for asset in waiting if asset.slot in voice.uses]
        if not judged:
            continue
        result = _run_check(voice.run, candidates, in_use, cancelled)
        if isinstance(result, str):  # once more: a busy computer, a slow first start
            result = _run_check(voice.run, candidates, in_use, cancelled)
        for asset in judged:
            earlier = verdicts[asset]
            if isinstance(earlier, str) or (isinstance(earlier, Inconclusive) and result is None):
                continue
            verdicts[asset] = (
                f"{voice.name}: {result}" if isinstance(result, str) and len(checks) > 1 else result
            )
    return verdicts


def _run_check(
    check: SelfCheck,
    new: Mapping[str, Path],
    old: Mapping[str, Path],
    cancelled: IsCancelled | None,
) -> CheckResult:
    if cancelled is not None and cancelled():
        raise Cancelled
    result = check(new, old)
    if result is not None and cancelled is not None and cancelled():
        raise Cancelled  # it failed because it was stopped: that says nothing about the build
    return result


def _is_update(asset: Asset, root: Path) -> bool:
    """True if ``asset`` is an archive with an older build in use, so a run can go on
    without its download."""
    return asset.archive is not None and build_in_use(asset, root) is not None


def _failed_here(sha256: str, slot: _Slot) -> bool:
    """The build failed with this Tomecast version, less than FAILURE_DAYS days ago. A record
    with no time, or a time in the future (the clock was wrong when it was written), counts as
    expired: being tried again is better than being blocked for good."""
    record = slot.failed.get(sha256)
    if record is None or record.get("tomecast") != __version__:
        return False
    try:
        when = float(record.get("at", "0"))
    except ValueError:
        return False
    return recent(when)


def recent(when: float) -> bool:
    """``when`` (seconds since 1970) was less than FAILURE_DAYS days ago, and isn't in the
    future (allowing a minute for clocks that disagree slightly)."""
    age = time.time() - when
    return -60 <= age < FAILURE_DAYS * 86400


def _chosen_by_newer(slot: _Slot) -> bool:
    """A newer Tomecast than this one chose the build in use (both are installed, and the
    newer one ran last): this one doesn't switch away from it, or the two would take turns."""
    return bool(slot.chosen_by) and version_key(slot.chosen_by) > version_key(__version__)


def version_key(version: str) -> tuple[int, ...]:
    """A Tomecast version as something to compare: ``"0.2.0"`` < ``"0.10.0"``, and a
    pre-release (``"0.2.0rc1"``) comes before its release."""
    match = re.match(r"(\d+(?:\.\d+)*)(.*)", version.strip())
    if not match:
        return (0,)
    release = tuple(int(part) for part in match.group(1).split("."))
    release += (0,) * (3 - len(release))
    rest = match.group(2)
    if not rest or rest.startswith(("+", ".post", "post")):
        return (*release, 1, 0)  # a release (or a local build of it)
    number = re.search(r"\d+", rest)
    return (*release, 0, int(number.group()) if number else 0)  # a pre-release or dev build


def _failure_record(reason: str, *, during: str) -> dict[str, str]:
    return {
        "tomecast": __version__,
        "reason": reason[-2000:],
        "at": f"{time.time():.0f}",
        "during": during,
    }


def _promote(asset: Asset, root: Path, *, proven: bool) -> None:
    """Make the pinned build of ``asset`` the one in use. The one to go back to is kept: the
    build being replaced if it had finished a book, else the one before it (so replacing a
    just-switched build that never ran never loses the last build known to work). It is never
    the new build itself: going "back" to the build in use would roll back nowhere."""
    slots = _load(root)
    old = _effective(asset, root, slots)
    new = _relative(build_folder(asset, root), root)
    old.failed.pop(asset.sha256, None)
    preferred = old.current if old.proven else old.previous
    fallback = next(
        (folder for folder in (preferred, old.current, old.previous) if folder and folder != new),
        None,
    )
    slots[asset.slot] = _Slot(
        current=new,
        previous=fallback,
        chosen_by=__version__,
        proven=proven,
        failed=old.failed,
    )
    _save(root, slots)


def _record_failure(slot_name: str, sha256: str, root: Path, reason: str, *, during: str) -> None:
    slots = _load(root)
    slot = slots.get(slot_name) or _Slot()
    slot.failed[sha256] = _failure_record(reason, during=during)
    slots[slot_name] = slot
    _save(root, slots)


def _tidy(assets: Sequence[Asset], root: Path) -> None:
    """Record the builds found on disk (from older versions) and remove what interrupted
    downloads and deletions left behind. Only called with the lock held."""
    slots = _load(root)
    for asset in assets:
        if asset.archive:
            slots[asset.slot] = _effective(asset, root, slots)
    _save(root, slots)
    folders = {(root / asset.path).parent for asset in assets}
    folders |= {root / asset.path for asset in assets if asset.archive}
    for folder in folders:
        for leftover in folder.glob(f"{_TEMP_PREFIX}*") if folder.is_dir() else ():
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover, ignore_errors=True)


def _clean_up(assets: Sequence[Asset], root: Path) -> None:
    """Delete builds that aren't needed. Each archive keeps the build in use; the pinned
    build if that isn't the one in use (waiting, failed and kept for a retry, or this
    version's own while a newer one's is in use); and the previous build, unless a pinned one
    is kept and the build in use has already finished a book. So there are at most two,
    except while a just-switched build hasn't finished a book yet and a newer one is waiting
    (three: the build to go back to is never deleted before then), or while a newer Tomecast
    owns the build in use (its fallback isn't ours to delete). Only called with the lock
    held."""
    slots = _load(root)
    for asset in assets:
        if not asset.archive or asset.slot not in slots:
            continue
        slot = slots[asset.slot]
        if slot.current is None:
            continue
        keep = {slot.current}
        pinned = _relative(build_folder(asset, root), root)
        other_kept = pinned != slot.current and build_sha256(root / pinned) == asset.sha256
        if other_kept:
            keep.add(pinned)
        has_previous = bool(slot.previous) and build_sha256(root / str(slot.previous)) is not None
        if has_previous and (not other_kept or not slot.proven or _chosen_by_newer(slot)):
            keep.add(str(slot.previous))
        else:
            slot.previous = None
        folder = root / asset.path
        others = list(folder.iterdir()) if folder.is_dir() else []
        others += [root / legacy for legacy in asset.replaces]
        for other in others:
            if other.is_symlink() or not other.is_dir() or _relative(other, root) in keep:
                continue
            _discard(other)
    _save(root, slots)


def _discard(folder: Path) -> None:
    """Delete a build folder so that it never looks complete half-way through: it's renamed
    to a ``.download-*`` leftover first (which is never used, and is removed next time if
    this is interrupted), then deleted."""
    trash = folder.parent / f"{_TEMP_PREFIX}old-{uuid.uuid4().hex[:12]}"
    try:
        folder.rename(trash)
    except OSError:
        return
    shutil.rmtree(trash, ignore_errors=True)


# --- installed.json, and the markers ----------------------------------------------------------


def _effective(asset: Asset, root: Path, slots: Mapping[str, _Slot]) -> _Slot:
    """``asset``'s record, with a build found on disk as the one in use when the record has
    none that is complete (a first run after upgrading from 0.1.x, or a lost state file)."""
    recorded = slots.get(asset.slot) or _Slot()
    if recorded.current and build_sha256(root / recorded.current):
        return _Slot(
            recorded.current,
            recorded.previous,
            recorded.chosen_by,
            recorded.proven,
            {sha: dict(record) for sha, record in recorded.failed.items()},
        )
    failed = {sha: dict(record) for sha, record in recorded.failed.items()}
    for found in _builds_on_disk(asset, root):
        return _Slot(_relative(found, root), None, "", True, failed)
    return _Slot(None, None, "", True, failed)


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
        slots[str(name)] = _Slot(
            current=_safe_relative(entry.get("current")),
            previous=_safe_relative(entry.get("previous")),
            chosen_by=str(entry.get("chosen_by", "")),
            proven=entry.get("proven", True) is not False,
            failed=_failures(entry.get("failed")),
        )
    return slots


def _failures(value: object) -> dict[str, dict[str, str]]:
    """``failed`` from installed.json. The first format held one record with its SHA-256
    inside; it is read as that build's record."""
    if not isinstance(value, dict):
        return {}
    if "sha256" in value:
        return {str(value["sha256"]): {k: str(v) for k, v in value.items() if k != "sha256"}}
    return {
        str(sha): {str(k): str(v) for k, v in record.items()}
        for sha, record in value.items()
        if isinstance(record, dict)
    }


def _save(root: Path, slots: Mapping[str, _Slot]) -> None:
    payload = {
        "format": 2,
        "slots": {
            name: {
                "current": slot.current,
                "previous": slot.previous,
                "chosen_by": slot.chosen_by,
                "proven": slot.proven,
                **({"failed": slot.failed} if slot.failed else {}),
            }
            for name, slot in sorted(slots.items())
        },
    }
    _write_atomic(root / _STATE, json.dumps(payload, indent=2) + "\n")


def _write_atomic(path: Path, text: str) -> None:
    """Write ``path`` all at once: a reader sees the old or the new contents, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
    os.replace(f.name, path)


def _verified_marker(target: Path) -> Path:
    return target.with_name(f".{target.name}{_VERIFIED}")


def _file_verified(asset: Asset, root: Path, *, hash_if_unsure: bool) -> bool:
    """True if the file of ``asset`` is the pinned one: its marker matches its size and
    modification time, or (``hash_if_unsure``) hashing it says so, which writes the marker.
    Without hashing, an unverified file counts if it has the pinned size."""
    target = root / asset.path
    try:
        stat = target.stat()
    except OSError:
        return False
    if not target.is_file():
        return False
    try:
        marker = json.loads(_verified_marker(target).read_text())
    except (OSError, ValueError):
        marker = None
    if isinstance(marker, dict) and marker == {
        "sha256": asset.sha256,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }:
        return True
    if not hash_if_unsure:
        return stat.st_size == asset.size
    if _sha256_of(target) != asset.sha256:
        return False
    with contextlib.suppress(OSError):  # a read-only folder: hash again next time
        mark_verified(asset, root)
    return True


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


class _Busy(Exception):
    """Another Tomecast held the install lock for longer than the caller would wait."""


@contextlib.contextmanager
def _locked(
    root: Path,
    cancelled: IsCancelled | None = None,
    wait: float | None = None,
    *,
    on_wait: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Hold the install lock, waiting while another Tomecast installs (``on_wait`` is called
    once if it has to). The wait never blocks for good: it asks ``cancelled`` between tries
    (raising :class:`Cancelled`), and gives up after ``wait`` seconds if that's set (raising
    :class:`_Busy`)."""
    root.mkdir(parents=True, exist_ok=True)
    deadline = None if wait is None else time.monotonic() + wait
    with (root / _LOCK).open("w") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if on_wait is not None:
                    on_wait()
                    on_wait = None
                if cancelled is not None and cancelled():
                    raise Cancelled from None
                if deadline is not None and time.monotonic() >= deadline:
                    raise _Busy from None
                time.sleep(_LOCK_POLL)
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
            # read1 returns what has arrived (read(n) waits for all n bytes), so a slow
            # connection still reports progress, and the reporter can stop the download.
            while block := response.read1(_DOWNLOAD_READ):
                f.write(block)
                digest.update(block)
                done += len(block)
                if progress:
                    progress(done, asset.size)
    except (OSError, http.client.HTTPException) as error:  # e.g. IncompleteRead
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
