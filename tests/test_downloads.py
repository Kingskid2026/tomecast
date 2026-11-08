import dataclasses
import fcntl
import hashlib
import http.client
import io
import json
import os
import shutil
import tarfile
import threading
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

import pytest

from tomecast import downloads
from tomecast.downloads import (
    FAILURE_DAYS,
    UPDATE_FAILED_NOTE,
    UPDATE_NOT_DOWNLOADED_NOTE,
    Asset,
    Cancelled,
    CheckResult,
    DownloadDeclined,
    DownloadError,
    Inconclusive,
    Prepared,
    ProgressCallback,
    VoiceCheck,
    build_folder,
    build_in_use,
    build_sha256,
    data_dir,
    ensure,
    failure,
    forget_failures,
    installed_sha256,
    is_installed,
    is_usable,
    mark_proven,
    needs_download,
    prepare,
    roll_back,
    version_key,
    waiting_check,
)
from tomecast.manifest import AUDIOCPP_VULKAN, ESPEAK_NG


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _file_asset(
    source: Path, *, path: str = "models/thing.bin", archive: Literal["tar.gz", "zip"] | None = None
) -> Asset:
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
    asset = _file_asset(archive, path="engines/audiocpp", archive="tar.gz")
    root = tmp_path / "data"

    folder = ensure(asset, root)

    assert (folder / "engine" / "bin" / "tts").read_bytes() == b"binary"
    assert is_installed(asset, root)


def test_archives_that_escape_their_folder_are_refused(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    _make_tar(archive, {"../../escaped": b"gotcha"})
    asset = _file_asset(archive, path="engines/evil", archive="tar.gz")
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


def test_zip_archives_like_wheels_are_extracted(tmp_path: Path) -> None:
    archive = tmp_path / "lib.whl"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("espeakng_loader/libespeak-ng.so.1", b"library")
        zf.writestr("espeakng_loader/espeak-ng-data/phontab", b"data")
    asset = _file_asset(archive, path="engines/espeak", archive="zip")

    folder = ensure(asset, tmp_path / "data")

    assert (folder / "espeakng_loader" / "libespeak-ng.so.1").read_bytes() == b"library"
    assert (folder / "espeakng_loader" / "espeak-ng-data" / "phontab").is_file()


def test_zip_archives_that_escape_their_folder_are_refused(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped", b"gotcha")
    asset = _file_asset(archive, path="engines/evil", archive="zip")

    with pytest.raises(DownloadError, match="unsafe path"):
        ensure(asset, tmp_path / "data")
    assert not (tmp_path / "escaped").exists()


# --- Switching to a new build only once it has worked ---------------------------------------


def _build(tmp_path: Path, name: str, server: bytes = b"#!/bin/sh\n", **fields: object) -> Asset:
    """An engine build: a tarball holding ``bin/server``, pinned like the real ones."""
    archive = tmp_path / "sources" / f"{name}.tar.gz"
    archive.parent.mkdir(exist_ok=True)
    _make_tar(archive, {"bin/server": server, "VERSION": name.encode()})
    data = archive.read_bytes()
    return Asset(
        **{  # type: ignore[arg-type]
            "id": "engine",
            "name": "test engine",
            "url": archive.as_uri(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "path": "engines/engine",
            "archive": "tar.gz",
            "replaces": ("engines/engine-0.1",),
            **fields,
        }
    )


class Check:
    """A self-check that records what it was asked to check, and gives these results in turn
    (the last one from then on)."""

    def __init__(self, *results: CheckResult) -> None:
        self.results = list(results) or [None]
        self.calls: list[tuple[dict[str, Path], dict[str, Path]]] = []
        self.during: Callable[[], None] | None = None  # runs while "checking"

    def __call__(self, new: Mapping[str, Path], old: Mapping[str, Path]) -> CheckResult:
        self.calls.append((dict(new), dict(old)))
        if self.during:
            self.during()
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def _version(root: Path, asset: Asset) -> str:
    """Which build is in use, by the VERSION file inside it."""
    folder = build_in_use(asset, root)
    assert folder is not None
    return (folder / "VERSION").read_text()


def _builds(root: Path) -> set[str]:
    """Every folder under engines/ (and engines/engine/), relative to the data folder."""
    found = set()
    for path in (root / "engines").rglob("*"):
        if path.is_dir() and path.name not in ("bin",):
            found.add(path.relative_to(root).as_posix())
    return found - {"engines/engine"}


def test_a_first_install_is_used_without_a_check(tmp_path: Path) -> None:
    root = tmp_path / "data"
    asset = _build(tmp_path, "v1")
    check = Check()

    prepared = prepare([asset], root, check=check)

    assert check.calls == []  # nothing to fall back to, so nothing to protect
    assert build_in_use(asset, root) == build_folder(asset, root)
    assert _version(root, asset) == "v1"
    assert is_installed(asset, root)
    assert prepared == Prepared(builds={"engine": build_folder(asset, root)})


def test_a_new_build_goes_beside_the_old_one_and_is_used_once_it_passes(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())
    old_folder = build_folder(old, root)
    check = Check()

    def while_checking() -> None:  # the old build is still the one in use, untouched
        assert build_in_use(new, root) == old_folder
        assert (old_folder / "bin" / "server").is_file()
        assert (build_folder(new, root) / "VERSION").read_text() == "v2"

    check.during = while_checking

    prepared = prepare([new], root, check=check)

    assert check.calls == [
        ({"engine": build_folder(new, root)}, {"engine": old_folder}),
    ]
    assert prepared.switched == (new,)
    assert prepared.notes == ()
    assert _version(root, new) == "v2"
    assert (old_folder / "VERSION").read_text() == "v1"  # kept, to go back to


def test_a_new_build_that_fails_its_check_is_not_used(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())

    prepared = prepare([new], root, check=Check("illegal instruction"))

    assert _version(root, new) == "v1"
    assert not is_installed(new, root) and is_usable(new, root)
    assert prepared.notes == (UPDATE_FAILED_NOTE,)
    assert prepared.held_back == (new,)
    assert failure(new, root) == "illegal instruction"
    assert UPDATE_FAILED_NOTE == (
        "The voice engine update didn't work on this computer, so Tomecast kept using the "
        "previous version."
    )


def test_a_failed_build_is_not_downloaded_or_checked_again_by_this_version(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())
    prepare([new], root, check=Check("broken"))
    Path(new.url.removeprefix("file://")).unlink()  # a second download would fail now
    check = Check()

    prepared = prepare([new], root, check=check)

    assert check.calls == []
    assert not needs_download(new, root)
    assert _version(root, new) == "v1"
    assert prepared.notes == ()  # said once, when it happened


def test_a_failed_build_is_tried_again_by_a_new_version_or_a_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())
    prepare([new], root, check=Check("broken"))

    monkeypatch.setattr(downloads, "__version__", "99.0.0")  # Tomecast was updated
    assert waiting_check(new, root)
    assert prepare([new], root, check=Check()).switched == (new,)

    third = _build(tmp_path, "v3")
    prepare([third], root, check=Check("broken"))
    forget_failures(root)  # `tomecast scan`
    check = Check()
    assert prepare([third], root, check=check).switched == (third,)
    assert len(check.calls) == 1


def test_an_interrupted_download_leaves_the_old_build_in_use(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())

    def stop(asset: Asset, number: int, count: int) -> ProgressCallback:
        def show(done: int, total: int) -> None:
            raise KeyboardInterrupt  # Ctrl-C mid-download

        return show

    with pytest.raises(KeyboardInterrupt):
        prepare([new], root, check=Check(), on_download=stop)

    assert _version(root, new) == "v1"
    assert not any((root / "engines").glob(".download-*"))
    assert build_sha256(build_folder(new, root)) is None
    assert prepare([new], root, check=Check()).switched == (new,)  # the next run finishes it


def test_leftovers_of_a_killed_download_are_removed(tmp_path: Path) -> None:
    root = tmp_path / "data"
    asset = _build(tmp_path, "v1")
    prepare([asset], root, check=Check())
    leftover = root / "engines" / ".download-x1y2z3"  # what `kill -9` mid-download leaves
    (leftover / "extracted").mkdir(parents=True)

    prepare([asset], root, check=Check())

    assert not leftover.exists()
    assert _version(root, asset) == "v1"


def test_a_corrupted_download_keeps_the_old_build_and_says_so(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old = _build(tmp_path, "v1")
    prepare([old], root, check=Check())
    tampered = dataclasses.replace(_build(tmp_path, "v2"), sha256="f" * 64)

    prepared = prepare([tampered], root, check=Check())

    assert _version(root, old) == "v1"
    assert prepared.notes == (UPDATE_NOT_DOWNLOADED_NOTE,)
    assert prepared.held_back == (tampered,)
    assert not (root / "engines" / "engine" / ("f" * 12)).exists()


def test_an_archive_that_wont_extract_keeps_the_old_build(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old = _build(tmp_path, "v1")
    prepare([old], root, check=Check())
    broken = tmp_path / "sources" / "broken.tar.gz"
    broken.write_bytes(b"not a tarball at all")
    data = broken.read_bytes()
    bad = dataclasses.replace(
        old, url=broken.as_uri(), sha256=hashlib.sha256(data).hexdigest(), size=len(data)
    )

    prepared = prepare([bad], root, check=Check())

    assert _version(root, old) == "v1"
    assert prepared.notes == (UPDATE_NOT_DOWNLOADED_NOTE,)
    assert _builds(root) == {_relative(build_folder(old, root), root)}


def test_a_broken_first_install_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "data"
    tampered = dataclasses.replace(_build(tmp_path, "v1"), sha256="f" * 64)

    with pytest.raises(DownloadError, match="checksum"):
        prepare([tampered], root, check=Check())  # nothing older to fall back to


def test_a_half_written_build_folder_is_replaced(tmp_path: Path) -> None:
    root = tmp_path / "data"
    asset = _build(tmp_path, "v1")
    half = build_folder(asset, root)
    (half / "bin").mkdir(parents=True)  # no marker: it never finished
    (half / "bin" / "server").write_bytes(b"trunc")

    prepare([asset], root, check=Check())

    assert _version(root, asset) == "v1"
    assert (half / "bin" / "server").read_bytes() == b"#!/bin/sh\n"


def test_an_update_the_user_declines_keeps_the_old_build(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())
    asked: list[tuple[list[Asset], bool]] = []

    def decline(missing: Sequence[Asset], updates: bool) -> bool:
        asked.append((list(missing), updates))
        return False

    prepared = prepare([new], root, check=Check(), consent=decline)

    assert asked == [([new], True)]
    assert _version(root, new) == "v1"
    assert prepared.held_back == (new,)


def test_declining_what_a_run_cant_do_without_stops_it(tmp_path: Path) -> None:
    asset = _build(tmp_path, "v1")
    with pytest.raises(DownloadDeclined):
        prepare([asset], tmp_path / "data", check=Check(), consent=lambda m, u: False)


def test_each_engine_keeps_at_most_two_builds(tmp_path: Path) -> None:
    root = tmp_path / "data"
    v1, v2, v3, v4 = (_build(tmp_path, f"v{n}") for n in range(1, 5))
    prepare([v1], root, check=Check())
    prepare([v2], root, check=Check())
    assert _builds(root) == {_relative(build_folder(a, root), root) for a in (v1, v2)}

    prepare([v3], root, check=Check())  # v1 is no longer needed
    assert _builds(root) == {_relative(build_folder(a, root), root) for a in (v2, v3)}

    prepare([v4], root, check=Check("broken"))  # v3 in use, v4 kept for a retry, v2 goes
    assert _builds(root) == {_relative(build_folder(a, root), root) for a in (v3, v4)}
    assert _version(root, v4) == "v3"


def test_new_builds_of_several_archives_are_checked_together(tmp_path: Path) -> None:
    root = tmp_path / "data"
    engine_1, engine_2 = _build(tmp_path, "e1"), _build(tmp_path, "e2")
    rules_1 = _build(tmp_path, "r1", id="rules", path="engines/rules", replaces=())
    rules_2 = _build(tmp_path, "r2", id="rules", path="engines/rules", replaces=())
    prepare([engine_1, rules_1], root, check=Check())
    check = Check("broken")

    prepare([engine_2, rules_2], root, check=check)

    together = (
        {"engine": build_folder(engine_2, root), "rules": build_folder(rules_2, root)},
        {"engine": build_folder(engine_1, root), "rules": build_folder(rules_1, root)},
    )
    assert check.calls == [together, together]  # failed, so tried once more
    assert (_version(root, engine_2), _version(root, rules_2)) == ("e1", "r1")


def test_only_new_builds_are_swapped_in_for_the_check(tmp_path: Path) -> None:
    root = tmp_path / "data"
    engine_1, engine_2 = _build(tmp_path, "e1"), _build(tmp_path, "e2")
    rules = _build(tmp_path, "r1", id="rules", path="engines/rules", replaces=())
    prepare([engine_1, rules], root, check=Check())
    check = Check()

    prepare([engine_2, rules], root, check=check)

    new, old = check.calls[0]
    assert new == {"engine": build_folder(engine_2, root), "rules": build_folder(rules, root)}
    assert old["rules"] == new["rules"]


def test_files_switch_on_their_checksum_alone(tmp_path: Path) -> None:
    root = tmp_path / "data"
    first, second = tmp_path / "m1.bin", tmp_path / "m2.bin"
    first.write_bytes(b"model one")
    second.write_bytes(b"model two")
    check = Check()

    prepare([_file_asset(first)], root, check=check)
    prepare([_file_asset(second)], root, check=check)

    assert (root / "models" / "thing.bin").read_bytes() == b"model two"
    assert check.calls == []  # models are data: the checksum is the check


def test_a_lost_state_file_finds_the_builds_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "data"
    asset = _build(tmp_path, "v1")
    prepare([asset], root, check=Check())
    (root / "installed.json").unlink()

    assert _version(root, asset) == "v1"
    assert is_installed(asset, root)


def test_without_a_record_an_unchecked_new_build_is_checked_before_use(tmp_path: Path) -> None:
    root = tmp_path / "data"
    legacy = root / "engines" / "engine-0.1"
    legacy.mkdir(parents=True)
    (legacy / ".tomecast-sha256").write_text("e" * 64)
    new = _build(tmp_path, "v2")
    ensure(new, root)  # on disk beside the old one, with no record of it ever being checked
    assert not (root / "installed.json").exists()
    assert build_in_use(new, root) == legacy  # the old build is the safer guess

    prepared = prepare([new], root, check=Check("broken"))

    assert build_in_use(new, root) == legacy
    assert prepared.notes == (UPDATE_FAILED_NOTE,)


def test_paths_in_the_state_file_cant_leave_the_data_folder(tmp_path: Path) -> None:
    root = tmp_path / "data"
    asset = _build(tmp_path, "v1")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / ".tomecast-sha256").write_text(asset.sha256)
    root.mkdir()
    (root / "installed.json").write_text(
        json.dumps({"format": 1, "slots": {"engine": {"current": "../elsewhere"}}})
    )

    assert build_in_use(asset, root) is None
    prepare([asset], root, check=Check())
    assert build_in_use(asset, root) == build_folder(asset, root)
    assert outside.is_dir()  # never touched


# --- The upgrade from Tomecast 0.1.2 ---------------------------------------------------------

# What 0.1.2 left: upstream's audio.cpp 0.9.4 Vulkan build, and the same eSpeak wheel as now.
UPSTREAM_VULKAN_SHA = "e5d8af8eb6500d87eb2ef94c1bc52be7dfc63b26012ff9abf0adce6f1f960208"


def _tomecast_0_1_2(root: Path) -> Path:
    legacy = root / "engines" / "audiocpp-0.9.4-vulkan"
    (legacy / "model_specs").mkdir(parents=True)
    (legacy / "audiocpp_server").write_bytes(b"upstream's build")
    (legacy / ".tomecast-sha256").write_text(UPSTREAM_VULKAN_SHA + "\n")
    espeak = root / "engines" / "espeak-ng-1.52"
    (espeak / "espeakng_loader").mkdir(parents=True)
    (espeak / ".tomecast-sha256").write_text(ESPEAK_NG.sha256 + "\n")
    return legacy


def _ours(tmp_path: Path) -> Asset:
    """The pinned Vulkan build (#27's), but downloaded from a local tarball."""
    archive = tmp_path / "ours.tar.gz"
    _make_tar(archive, {"audiocpp_server": b"our build", "BUILD-INFO.txt": b"tc1"})
    data = archive.read_bytes()
    return dataclasses.replace(
        AUDIOCPP_VULKAN,
        url=archive.as_uri(),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


def test_upgrading_from_0_1_2_keeps_upstreams_build_until_ours_passes(tmp_path: Path) -> None:
    root = tmp_path / "data"
    legacy = _tomecast_0_1_2(root)
    ours = _ours(tmp_path)
    # Before anything runs, the old build is the one in use, and only ours needs downloading
    # (eSpeak is already there: its URL is real, so a download would fail in this test).
    assert build_in_use(ours, root) == legacy
    assert needs_download(ours, root) and not needs_download(ESPEAK_NG, root)
    check = Check()

    def while_checking() -> None:  # upstream's build is still there, and still in use
        assert (legacy / "audiocpp_server").read_bytes() == b"upstream's build"
        assert build_in_use(ours, root) == legacy

    check.during = while_checking

    prepared = prepare([ours, ESPEAK_NG], root, check=check)

    ours_folder = build_folder(ours, root)
    assert check.calls == [
        (
            {"audiocpp-vulkan": ours_folder, "espeak-ng": root / "engines/espeak-ng-1.52"},
            {"audiocpp-vulkan": legacy, "espeak-ng": root / "engines/espeak-ng-1.52"},
        )
    ]
    assert prepared.switched == (ours,)
    assert build_in_use(ours, root) == ours_folder
    assert ours_folder == root / "engines" / "audiocpp-vulkan" / ours.sha256[:12]
    assert (legacy / "audiocpp_server").read_bytes() == b"upstream's build"  # the fallback
    assert build_in_use(ESPEAK_NG, root) == root / "engines" / "espeak-ng-1.52"  # still used


def test_upgrading_from_0_1_2_stays_on_upstreams_build_if_ours_fails_here(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    legacy = _tomecast_0_1_2(root)
    ours = _ours(tmp_path)

    prepared = prepare([ours, ESPEAK_NG], root, check=Check("no Vulkan driver"))

    assert build_in_use(ours, root) == legacy
    assert installed_sha256(ours, root) == UPSTREAM_VULKAN_SHA
    assert prepared.notes == (UPDATE_FAILED_NOTE,)
    assert (legacy / "audiocpp_server").read_bytes() == b"upstream's build"
    assert build_sha256(build_folder(ours, root)) == ours.sha256  # kept for a retry


def test_after_the_upgrade_the_old_build_goes_once_a_newer_one_passes(tmp_path: Path) -> None:
    root = tmp_path / "data"
    legacy = _tomecast_0_1_2(root)
    ours = _ours(tmp_path)
    prepare([ours, ESPEAK_NG], root, check=Check())
    archive = tmp_path / "next.tar.gz"
    _make_tar(archive, {"audiocpp_server": b"the next build"})
    data = archive.read_bytes()
    after = dataclasses.replace(
        ours, url=archive.as_uri(), sha256=hashlib.sha256(data).hexdigest(), size=len(data)
    )

    prepare([after, ESPEAK_NG], root, check=Check())

    assert not legacy.exists()
    assert build_sha256(build_folder(ours, root)) == ours.sha256  # now the fallback
    assert build_in_use(after, root) == build_folder(after, root)


# --- After the review: rollback, fairness, two versions, safe deletes ----------------------------


def _switched(tmp_path: Path, root: Path) -> tuple[Asset, Asset, Prepared]:
    """v1 in use, then v2 switched to after passing its check (it hasn't run a book yet)."""
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())
    prepared = prepare([new], root, check=Check())
    assert prepared.switched == (new,)
    return old, new, prepared


def test_a_switched_build_that_fails_a_real_run_is_rolled_back(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old, new, prepared = _switched(tmp_path, root)
    assert prepared.builds == {"engine": build_folder(new, root)}

    assert roll_back(root, prepared.builds, "the voice engine keeps crashing")

    assert _version(root, new) == "v1"  # the next run uses the build that worked
    assert failure(new, root) == "the voice engine keeps crashing"
    check = Check()
    again = prepare([new], root, check=check)
    assert check.calls == []  # the bad build isn't picked (or downloaded) again
    assert again.builds == {"engine": build_folder(old, root)}
    assert build_sha256(build_folder(new, root)) == new.sha256  # kept, for a retry later


def test_a_build_that_has_finished_a_run_is_not_rolled_back(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _, new, prepared = _switched(tmp_path, root)
    mark_proven(root, prepared.builds)

    assert not roll_back(root, prepared.builds, "out of graphics memory")

    assert _version(root, new) == "v2"
    assert failure(new, root) is None


def test_only_the_build_the_run_used_is_rolled_back(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old, new, _ = _switched(tmp_path, root)

    assert not roll_back(root, {"engine": build_folder(old, root)}, "it failed")
    assert _version(root, new) == "v2"


def test_a_first_install_has_nothing_to_roll_back_to(tmp_path: Path) -> None:
    root = tmp_path / "data"
    asset = _build(tmp_path, "v1")
    prepared = prepare([asset], root, check=Check())

    assert not roll_back(root, prepared.builds, "it failed")
    assert _version(root, asset) == "v1"


def test_a_failed_check_is_tried_once_more_before_it_counts(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())
    check = Check("the engine didn't start within 120s", None)  # a slow first start

    prepared = prepare([new], root, check=check)

    assert len(check.calls) == 2
    assert prepared.switched == (new,)
    assert prepared.notes == ()


def test_a_recorded_failure_expires_after_a_few_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())
    prepare([new], root, check=Check("busy"))
    assert not waiting_check(new, root)

    now = time.time()
    monkeypatch.setattr("time.time", lambda: now + (FAILURE_DAYS - 0.5) * 86400)
    assert not waiting_check(new, root)
    monkeypatch.setattr("time.time", lambda: now + (FAILURE_DAYS + 0.5) * 86400)
    assert waiting_check(new, root)
    assert prepare([new], root, check=Check()).switched == (new,)


def test_no_verdict_switches_nothing_records_nothing_and_checks_again(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())
    unsure = Check(Inconclusive("the graphics card didn't work with any build"))

    prepared = prepare([new], root, check=unsure)

    assert len(unsure.calls) == 1  # no retry: it isn't a failure
    assert (prepared.switched, prepared.notes) == ((), ())
    assert _version(root, new) == "v1"
    assert failure(new, root) is None
    assert waiting_check(new, root)
    assert prepare([new], root, check=Check()).switched == (new,)  # the next run


def test_each_build_is_judged_by_the_voices_that_use_it_and_they_switch_together(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    engine_1, engine_2 = _build(tmp_path, "e1"), _build(tmp_path, "e2")
    rules_1 = _build(tmp_path, "r1", id="rules", path="engines/rules", replaces=())
    rules_2 = _build(tmp_path, "r2", id="rules", path="engines/rules", replaces=())
    prepare([engine_1, rules_1], root, check=Check())
    kokoro, ming = Check(), Check("Ming made silence")
    checks = [
        VoiceCheck("Kokoro", frozenset({"engine", "rules"}), kokoro),
        VoiceCheck("Ming", frozenset({"engine"}), ming),
    ]

    prepared = prepare([engine_2, rules_2], root, check=checks)

    assert len(kokoro.calls) == 1 and len(ming.calls) == 2  # Ming's failure was retried
    assert failure(engine_2, root) == "Ming: Ming made silence"
    assert failure(rules_2, root) is None  # only the engine is blamed...
    assert (_version(root, engine_2), _version(root, rules_2)) == ("e1", "r1")  # ...but neither
    assert prepared.notes == (UPDATE_FAILED_NOTE,)

    # The next run checks the new eSpeak-like build with the engine in use.
    kokoro_again = Check()
    checks = [
        VoiceCheck("Kokoro", frozenset({"engine", "rules"}), kokoro_again),
        VoiceCheck("Ming", frozenset({"engine"}), Check()),
    ]
    assert prepare([engine_2, rules_2], root, check=checks).switched == (rules_2,)
    new, _ = kokoro_again.calls[0]
    assert new == {"engine": build_folder(engine_1, root), "rules": build_folder(rules_2, root)}


def test_a_build_no_voice_uses_is_never_switched_to_unchecked(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())
    ming = Check()

    prepared = prepare([new], root, check=[VoiceCheck("Ming", frozenset({"other"}), ming)])

    assert ming.calls == []
    assert prepared.switched == ()
    assert _version(root, new) == "v1"


def test_an_older_tomecast_never_switches_away_from_a_newer_ones_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    ours, theirs = _build(tmp_path, "ours"), _build(tmp_path, "theirs")
    monkeypatch.setattr(downloads, "__version__", "0.2.0")
    prepare([ours], root, check=Check())
    monkeypatch.setattr(downloads, "__version__", "0.3.0")  # the newer CLI runs
    assert prepare([theirs], root, check=Check()).switched == (theirs,)

    monkeypatch.setattr(downloads, "__version__", "0.2.0")  # the older window still open
    check = Check()
    prepared = prepare([ours], root, check=check)

    assert check.calls == []  # no check, no switch back
    assert _version(root, ours) == "theirs"
    assert prepared.builds == {"engine": build_folder(ours, root)}  # it runs its own build
    monkeypatch.setattr(downloads, "__version__", "0.3.0")
    again = prepare([theirs], root, check=check)
    assert (check.calls, again.switched) == ([], ())  # and the newer one has nothing to undo
    assert again.builds == {"engine": build_folder(theirs, root)}


def test_an_older_tomecast_without_its_own_build_uses_the_newer_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    theirs = _build(tmp_path, "theirs")
    monkeypatch.setattr(downloads, "__version__", "0.3.0")
    prepare([theirs], root, check=Check())
    monkeypatch.setattr(downloads, "__version__", "0.2.0")
    ours = _build(tmp_path, "ours")
    Path(ours.url.removeprefix("file://")).unlink()  # a download would fail

    prepared = prepare([ours], root, check=Check())

    assert not needs_download(ours, root)
    assert prepared.builds == {"engine": build_folder(theirs, root)}


def test_versions_compare_as_numbers_with_pre_releases_first() -> None:
    assert version_key("0.2.0") < version_key("0.10.0")
    assert version_key("0.2.0rc1") < version_key("0.2.0") < version_key("0.2.1")
    assert version_key("1.0") == version_key("1.0.0")


def test_a_half_deleted_build_never_looks_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    v1, v2, v3 = (_build(tmp_path, f"v{n}") for n in range(1, 4))
    prepare([v1], root, check=Check())
    prepare([v2], root, check=Check())
    real_rmtree = shutil.rmtree

    def killed_halfway(path: object, *args: object, **kwargs: object) -> None:
        if not Path(str(path)).name.startswith(".download-old-"):
            return real_rmtree(path, *args, **kwargs)  # type: ignore[call-overload,no-any-return]
        (Path(str(path)) / "VERSION").unlink()  # some files gone, then the power cut
        raise SystemExit("killed")

    monkeypatch.setattr("shutil.rmtree", killed_halfway)
    with pytest.raises(SystemExit):
        prepare([v3], root, check=Check())  # v3 passes; deleting v1 is interrupted
    monkeypatch.setattr("shutil.rmtree", real_rmtree)

    v1_folder = build_folder(v1, root)
    assert not v1_folder.exists()  # renamed away before anything was deleted
    leftovers = list((root / "engines" / "engine").glob(".download-*"))
    assert len(leftovers) == 1 and build_sha256(leftovers[0]) == v1.sha256
    assert _version(root, v3) == "v3"
    prepare([v3], root, check=Check())  # the next run tidies it away
    assert not any((root / "engines" / "engine").glob(".download-*"))


def test_a_download_cut_off_mid_way_keeps_the_old_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())

    class CutOff(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise http.client.IncompleteRead(b"part", 100)

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: CutOff())

    prepared = prepare([new], root, check=Check())

    assert prepared.notes == (UPDATE_NOT_DOWNLOADED_NOTE,)
    assert _version(root, new) == "v1"


def test_waiting_for_another_tomecast_can_be_cancelled(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    asset = _build(tmp_path, "v1")
    with (root / ".install.lock").open("w") as other:  # another Tomecast is installing
        fcntl.flock(other, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(Cancelled):
            prepare(
                [asset], root, check=Check(), cancelled=lambda: time.monotonic() > started + 0.3
            )
        assert time.monotonic() - started < 3
    assert build_in_use(asset, root) is None


def test_waiting_for_another_tomecast_goes_on_once_it_is_done(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    asset = _build(tmp_path, "v1")
    other = (root / ".install.lock").open("w")
    fcntl.flock(other, fcntl.LOCK_EX)
    threading.Timer(0.4, other.close).start()  # it finishes and lets go

    prepare([asset], root, check=Check())

    assert _version(root, asset) == "v1"


def test_a_check_stopped_by_the_user_records_nothing(tmp_path: Path) -> None:
    root = tmp_path / "data"
    old, new = _build(tmp_path, "v1"), _build(tmp_path, "v2")
    prepare([old], root, check=Check())
    stopped = False

    def stop_during_check() -> None:
        nonlocal stopped
        stopped = True  # Cancel pressed while the engine was starting

    check = Check("the voice engine stopped while starting")
    check.during = stop_during_check

    with pytest.raises(Cancelled):
        prepare([new], root, check=check, cancelled=lambda: stopped)

    assert failure(new, root) is None
    assert waiting_check(new, root)


def test_a_verified_model_is_not_hashed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"a big model")
    asset = _file_asset(source)
    root = tmp_path / "data"
    ensure(asset, root)  # verified while downloading: its marker is written
    hashed: list[Path] = []
    real = downloads._sha256_of

    def counting(path: Path) -> str:
        hashed.append(path)
        return real(path)

    monkeypatch.setattr(downloads, "_sha256_of", counting)

    assert is_installed(asset, root) and not needs_download(asset, root)
    assert hashed == []
    target = root / asset.path
    target.write_bytes(b"a big modxl")  # changed (same size, new modification time)
    os.utime(target, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert not is_installed(asset, root)
    assert hashed == [target]


def test_the_gui_check_never_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"a big model")
    asset = _file_asset(source)
    root = tmp_path / "data"
    target = root / asset.path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"a big model")  # put there by Tomecast 0.1.x: no marker yet
    monkeypatch.setattr(downloads, "_sha256_of", lambda path: pytest.fail("hashed"))

    assert not needs_download(asset, root, verify=False)  # the pinned size: counts for now
    target.write_bytes(b"short")
    assert needs_download(asset, root, verify=False)
