import dataclasses
import hashlib
import io
import json
import tarfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

import pytest

from tomecast import downloads
from tomecast.downloads import (
    UPDATE_FAILED_NOTE,
    UPDATE_NOT_DOWNLOADED_NOTE,
    Asset,
    DownloadDeclined,
    DownloadError,
    Prepared,
    ProgressCallback,
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
    needs_download,
    prepare,
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
    """A self-check that records what it was asked to check, and passes or fails."""

    def __init__(self, problem: str | None = None) -> None:
        self.problem = problem
        self.calls: list[tuple[dict[str, Path], dict[str, Path]]] = []
        self.during: Callable[[], None] | None = None  # runs while "checking"

    def __call__(self, new: Mapping[str, Path], old: Mapping[str, Path]) -> str | None:
        self.calls.append((dict(new), dict(old)))
        if self.during:
            self.during()
        return self.problem


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
    assert prepared == Prepared()


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

    assert check.calls == [
        (
            {"engine": build_folder(engine_2, root), "rules": build_folder(rules_2, root)},
            {"engine": build_folder(engine_1, root), "rules": build_folder(rules_1, root)},
        )
    ]
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
