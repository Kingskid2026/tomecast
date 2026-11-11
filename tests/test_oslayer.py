import ctypes
import importlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import FrameType

import pytest

from tomecast import oslayer
from tomecast.oslayer import FileLock

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only behaviour")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")


# --- Folders --------------------------------------------------------------------------------


@linux_only
def test_data_dir_is_the_one_tomecast_has_always_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tomecast.downloads import data_dir as downloads_data_dir

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert oslayer.data_dir() == tmp_path / "tomecast" == downloads_data_dir()
    monkeypatch.delenv("XDG_DATA_HOME")
    assert oslayer.data_dir() == Path.home() / ".local" / "share" / "tomecast"
    assert oslayer.data_dir() == downloads_data_dir()


@linux_only
def test_config_dir_is_where_qsettings_keeps_the_window_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert oslayer.config_dir() == tmp_path / "Tomecast"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert oslayer.config_dir() == Path.home() / ".config" / "Tomecast"


@windows_only
def test_windows_folders_are_under_local_app_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert oslayer.data_dir() == tmp_path / "Tomecast"
    assert oslayer.config_dir() == tmp_path / "Tomecast" / "config"


def test_xdg_data_home_keeps_any_value_it_is_given() -> None:
    home = PurePosixPath("/home/reader")
    assert oslayer.xdg_data_home({}, home) == home / ".local" / "share"
    assert oslayer.xdg_data_home({"XDG_DATA_HOME": ""}, home) == home / ".local" / "share"
    assert oslayer.xdg_data_home({"XDG_DATA_HOME": "/data"}, home) == PurePosixPath("/data")
    # Relative values have always been used as they are; changing that would move downloads.
    assert oslayer.xdg_data_home({"XDG_DATA_HOME": "rel"}, home) == PurePosixPath("rel")


def test_xdg_config_home_ignores_relative_values() -> None:
    home = PurePosixPath("/home/reader")
    assert oslayer.xdg_config_home({}, home) == home / ".config"
    assert oslayer.xdg_config_home({"XDG_CONFIG_HOME": "/cfg"}, home) == PurePosixPath("/cfg")
    assert oslayer.xdg_config_home({"XDG_CONFIG_HOME": "cfg"}, home) == home / ".config"


def test_local_app_data_falls_back_to_the_profile_folder() -> None:
    home = PureWindowsPath(r"C:\Users\Reader")
    local = PureWindowsPath(r"D:\Profiles\Reader\AppData\Local")
    assert oslayer.local_app_data({"LOCALAPPDATA": str(local)}, home) == local
    assert oslayer.local_app_data({}, home) == home / "AppData" / "Local"
    assert oslayer.local_app_data({"LOCALAPPDATA": ""}, home) == home / "AppData" / "Local"
    assert oslayer.local_app_data({"LOCALAPPDATA": "Local"}, home) == home / "AppData" / "Local"


# --- File locks -----------------------------------------------------------------------------


def test_only_one_lock_holds_a_file_at_a_time(tmp_path: Path) -> None:
    first, second = FileLock(tmp_path / ".lock"), FileLock(tmp_path / ".lock")
    assert first.acquire()
    assert first.held
    assert not second.acquire()  # even in the same process
    assert not second.held
    first.release()
    assert not first.held
    assert second.acquire()  # free again
    second.release()
    second.release()  # releasing twice is fine


def test_a_lock_cannot_be_taken_twice_by_the_same_object(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / ".lock")
    assert lock.acquire()
    with pytest.raises(RuntimeError, match="already locked"):
        lock.acquire()
    lock.release()


def test_the_lock_file_is_created_and_never_truncated(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    lock = FileLock(path)
    assert lock.acquire()
    lock.release()
    assert path.is_file()
    path.write_bytes(b"left by someone")
    assert lock.acquire()
    lock.release()
    assert path.read_bytes() == b"left by someone"


def test_a_missing_folder_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FileLock(tmp_path / "missing" / ".lock").acquire()


@pytest.mark.posix_only
def test_it_conflicts_with_the_flock_older_tomecasts_take(tmp_path: Path) -> None:
    fcntl = importlib.import_module("fcntl")  # not `import`: mypy on Windows has no fcntl

    path = tmp_path / ".lock"
    with path.open("w") as older:  # an older Tomecast holding the lock
        fcntl.flock(older, fcntl.LOCK_EX)
        assert not FileLock(path).acquire()
    lock = FileLock(path)
    assert lock.acquire()
    with path.open("w") as older, pytest.raises(BlockingIOError):
        fcntl.flock(older, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock.release()


# --- Child processes that stop with Tomecast --------------------------------------------------

# The grandchild: holds a lock until it's stopped, so the test can tell when it has gone.
_HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from tomecast.oslayer import FileLock

    lock = FileLock(Path(sys.argv[1]))
    assert lock.acquire()
    Path(sys.argv[1] + ".ready").write_text("")
    time.sleep(60)
    """
)

# Tomecast: starts the holder the way the engine would be started, then dies without
# stopping it.
_PARENT = textwrap.dedent(
    """
    import os, subprocess, sys, time
    from pathlib import Path
    from tomecast import oslayer

    path = Path(sys.argv[1])
    child = subprocess.Popen(
        [sys.executable, "-c", sys.argv[2], str(path)], **oslayer.child_options()
    )
    oslayer.adopt_child(child)
    deadline = time.monotonic() + 30
    while not Path(str(path) + ".ready").exists():
        if child.poll() is not None or time.monotonic() > deadline:
            print("holder failed")
            os._exit(1)
        time.sleep(0.05)
    print(child.pid)
    print("held" if not oslayer.FileLock(path).acquire() else "not held", flush=True)
    os._exit(0)  # dies like a crash: no clean-up, no stopping the child
    """
)


@pytest.mark.skipif(
    sys.platform not in ("linux", "win32"), reason="only Linux and Windows tie children"
)
def test_a_child_stops_when_tomecast_dies(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    result = subprocess.run(
        [sys.executable, "-c", _PARENT, str(path), _HOLDER],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = result.stdout.split()
    assert result.returncode == 0, result.stdout + result.stderr
    pid, held = int(lines[0]), lines[1]
    assert held == "held"  # the lock works across processes too
    lock = FileLock(path)
    deadline = time.monotonic() + 15
    try:
        while not lock.acquire():
            assert time.monotonic() < deadline, "the child outlived Tomecast"
            time.sleep(0.1)
    finally:
        if not lock.held:  # don't leave it running for a minute
            os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    lock.release()


def test_child_options_suit_this_system() -> None:
    options = oslayer.child_options()
    if sys.platform == "win32":
        assert options["preexec_fn"] is None
        assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
    elif sys.platform == "linux":
        assert callable(options["preexec_fn"])
        assert options["creationflags"] == 0
    # The options are accepted as they are.
    process = subprocess.Popen([sys.executable, "-c", "pass"], **options)
    oslayer.adopt_child(process)
    assert process.wait(timeout=30) == 0


def test_adopting_a_child_that_already_exited_is_fine() -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], **oslayer.child_options())
    process.wait(timeout=30)
    oslayer.adopt_child(process)


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="64-bit layout")
def test_job_object_structures_match_windows_x64() -> None:
    assert ctypes.sizeof(oslayer.JobObjectBasicLimitInformation) == 64
    assert ctypes.sizeof(oslayer.IoCounters) == 48
    assert ctypes.sizeof(oslayer.JobObjectExtendedLimitInformation) == 144
    assert oslayer.JobObjectBasicLimitInformation.LimitFlags.offset == 16


# --- Stop signals ---------------------------------------------------------------------------


def test_stop_signals_suit_this_system() -> None:
    if sys.platform == "win32":
        assert oslayer.stop_signals() == (signal.SIGINT, signal.SIGBREAK)
    else:
        assert oslayer.stop_signals() == (signal.SIGTERM, signal.SIGINT)


def test_on_signals_handles_them_and_puts_the_old_handlers_back() -> None:
    seen: list[int] = []

    def handler(signum: int, frame: FrameType | None) -> None:
        seen.append(signum)

    before = signal.getsignal(signal.SIGINT)
    with oslayer.on_signals([signal.SIGINT], handler):
        signal.raise_signal(signal.SIGINT)
    assert seen == [signal.SIGINT]
    assert signal.getsignal(signal.SIGINT) is before


def test_on_signals_puts_the_old_handlers_back_after_an_error() -> None:
    before = signal.getsignal(signal.SIGINT)
    with (
        pytest.raises(KeyboardInterrupt),
        oslayer.on_signals([signal.SIGINT], lambda *_: None),
    ):
        raise KeyboardInterrupt
    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.posix_only
def test_sigterm_reaches_the_handler() -> None:
    seen: list[int] = []
    with oslayer.on_signals(oslayer.stop_signals(), lambda signum, _: seen.append(signum)):
        os.kill(os.getpid(), signal.SIGTERM)
    assert seen == [signal.SIGTERM]
