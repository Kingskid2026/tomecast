import ctypes
import gc
import importlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
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

    def data(environ: dict[str, str]) -> PurePath:
        return oslayer.xdg_data_home(environ, lambda: home, PurePosixPath)

    assert data({}) == home / ".local" / "share"
    assert data({"XDG_DATA_HOME": ""}) == home / ".local" / "share"
    assert data({"XDG_DATA_HOME": "/data"}) == PurePosixPath("/data")
    # Relative values have always been used as they are; changing that would move downloads.
    assert data({"XDG_DATA_HOME": "rel"}) == PurePosixPath("rel")


def test_xdg_config_home_ignores_relative_values() -> None:
    home = PurePosixPath("/home/reader")

    def config(environ: dict[str, str]) -> PurePath:
        return oslayer.xdg_config_home(environ, lambda: home, PurePosixPath)

    assert config({}) == home / ".config"
    assert config({"XDG_CONFIG_HOME": "/cfg"}) == PurePosixPath("/cfg")
    assert config({"XDG_CONFIG_HOME": "cfg"}) == home / ".config"


def test_local_app_data_falls_back_to_the_profile_folder() -> None:
    home = PureWindowsPath(r"C:\Users\Reader")
    local = PureWindowsPath(r"D:\Profiles\Reader\AppData\Local")

    def app_data(environ: dict[str, str]) -> PurePath:
        return oslayer.local_app_data(environ, lambda: home, PureWindowsPath)

    assert app_data({"LOCALAPPDATA": str(local)}) == local
    assert app_data({}) == home / "AppData" / "Local"
    assert app_data({"LOCALAPPDATA": ""}) == home / "AppData" / "Local"
    assert app_data({"LOCALAPPDATA": "Local"}) == home / "AppData" / "Local"


def test_the_folders_need_no_home_folder_when_the_variables_are_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Path.home() raises with HOME unset and no passwd entry (a container run with an
    arbitrary uid); a set XDG_DATA_HOME has always been enough."""

    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    assert oslayer.data_dir().parent == tmp_path
    assert tmp_path in oslayer.config_dir().parents
    monkeypatch.delenv("LOCALAPPDATA" if sys.platform == "win32" else "XDG_DATA_HOME")
    with pytest.raises(RuntimeError, match="home directory"):
        oslayer.data_dir()  # the fallback does need it


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


def test_a_lock_dropped_while_held_is_let_go(tmp_path: Path) -> None:
    dropped = FileLock(tmp_path / ".lock")
    assert dropped.acquire()
    del dropped  # never released, as after an exception before the caller's `try`
    gc.collect()
    lock = FileLock(tmp_path / ".lock")
    assert lock.acquire()
    lock.release()


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

# Tomecast: starts the holder the way the engine is started, then dies without stopping it.
# The holder gets no stdout or stderr from it: if it inherited the test's pipes, a holder
# that outlived Tomecast would keep subprocess.run() waiting for the end of the output.
_PARENT = textwrap.dedent(
    """
    import os, subprocess, sys, time
    from pathlib import Path
    from tomecast import oslayer

    path = Path(sys.argv[1])
    child = oslayer.start_tied(
        [sys.executable, "-c", sys.argv[2], str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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


def test_start_tied_runs_the_child_with_the_given_options() -> None:
    process = oslayer.start_tied(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    output, _ = process.communicate(b"spoken", timeout=30)
    assert process.returncode == 0
    assert output.strip() == b"SPOKEN"  # it ran (on Windows: it was resumed)


def test_start_tied_sets_preexec_fn_itself() -> None:
    with pytest.raises(ValueError, match="preexec_fn"):
        oslayer.start_tied([sys.executable, "-c", "pass"], preexec_fn=lambda: None)


def test_a_child_that_cant_start_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        oslayer.start_tied([str(tmp_path / "no-such-program")])


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="64-bit layout")
def test_windows_structures_match_windows_x64() -> None:
    assert ctypes.sizeof(oslayer.JobObjectBasicLimitInformation) == 64
    assert ctypes.sizeof(oslayer.IoCounters) == 48
    assert ctypes.sizeof(oslayer.JobObjectExtendedLimitInformation) == 144
    assert oslayer.JobObjectBasicLimitInformation.LimitFlags.offset == 16
    assert ctypes.sizeof(oslayer.ThreadEntry32) == 28
    assert oslayer.ThreadEntry32.th32OwnerProcessID.offset == 12


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
