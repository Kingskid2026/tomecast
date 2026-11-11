"""What Tomecast does differently on Linux and on Windows, behind one set of functions.

Each function has a POSIX version and a Windows version, picked with ``sys.platform`` checks
that mypy understands, so ``mypy`` on Linux checks the Linux code and ``mypy --platform
win32`` (or mypy on a Windows runner) checks the Windows code. Nothing here is imported for
its side effects, and importing it loads nothing platform-specific beyond the standard
library.

* :class:`FileLock`: an exclusive lock on a lock file (``flock`` on POSIX, ``msvcrt.locking``
  on Windows), for the work-folder and install locks.
* :func:`start_tied`: start a child process (the voice engine) that stops if Tomecast itself
  dies (``PR_SET_PDEATHSIG`` on Linux, a Job Object that kills its processes when closed on
  Windows).
* :func:`data_dir` and :func:`config_dir`: where Tomecast keeps its files (XDG on Linux,
  ``%LOCALAPPDATA%`` on Windows).
* :func:`stop_signals` and :func:`on_signals`: the signals that ask Tomecast to stop tidily.

The module is named ``oslayer`` rather than ``platform`` so it never shadows the standard
library's :mod:`platform`.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path, PurePath
from types import FrameType
from typing import Any

if sys.platform == "win32":
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl

IS_WINDOWS = sys.platform == "win32"


# --- Folders --------------------------------------------------------------------------------

HomeFolder = Callable[[], PurePath]


def data_dir() -> Path:
    """Tomecast's data folder (engines, models, work folders, ``installed.json``).

    Linux: ``$XDG_DATA_HOME/tomecast``, or ``~/.local/share/tomecast``. Windows:
    ``%LOCALAPPDATA%\\Tomecast``, or ``~\\AppData\\Local\\Tomecast`` if that isn't set.
    """
    if sys.platform == "win32":
        return Path(local_app_data(os.environ, Path.home, Path)) / "Tomecast"
    return Path(xdg_data_home(os.environ, Path.home, Path)) / "tomecast"


def config_dir() -> Path:
    """Where the window's settings live.

    Linux: ``$XDG_CONFIG_HOME/Tomecast``, or ``~/.config/Tomecast``: the folder ``QSettings``
    already uses (organization "Tomecast"), so nothing moves. Windows:
    ``%LOCALAPPDATA%\\Tomecast\\config``. ``QSettings`` uses the registry on Windows unless
    it's told to use an INI file; which one Tomecast uses there is still to be decided (#39).
    """
    if sys.platform == "win32":
        return Path(local_app_data(os.environ, Path.home, Path)) / "Tomecast" / "config"
    return Path(xdg_config_home(os.environ, Path.home, Path)) / "Tomecast"


# The home folder is only looked up when it's needed: Path.home() raises if HOME is unset and
# the user has no passwd entry (a container run with an arbitrary uid), and a set
# XDG_DATA_HOME has always been enough for Tomecast to run there.


def xdg_data_home(
    environ: Mapping[str, str], home: HomeFolder, path: type[PurePath] = Path
) -> PurePath:
    """``$XDG_DATA_HOME``, or ``~/.local/share`` (``home()`` is only called then).

    Any non-empty value is used, even a relative one: that's what Tomecast has always done
    for its data folder, and changing it would move someone's downloads.
    """
    configured = environ.get("XDG_DATA_HOME")
    return path(configured) if configured else home() / ".local" / "share"


def xdg_config_home(
    environ: Mapping[str, str], home: HomeFolder, path: type[PurePath] = Path
) -> PurePath:
    """``$XDG_CONFIG_HOME`` if it's an absolute path (as the XDG spec and Qt require), or
    ``~/.config`` (``home()`` is only called then)."""
    configured = environ.get("XDG_CONFIG_HOME")
    if configured and path(configured).is_absolute():
        return path(configured)
    return home() / ".config"


def local_app_data(
    environ: Mapping[str, str], home: HomeFolder, path: type[PurePath] = Path
) -> PurePath:
    """``%LOCALAPPDATA%`` if it's an absolute path, or ``~\\AppData\\Local`` (``home()`` is
    only called then)."""
    configured = environ.get("LOCALAPPDATA")
    if configured and path(configured).is_absolute():
        return path(configured)
    return home() / "AppData" / "Local"


# --- File locks -----------------------------------------------------------------------------


class FileLock:
    """An exclusive lock on a lock file, taken without waiting.

    The file is created if it's missing and never truncated. The lock lasts until
    :meth:`release`, or until the ``FileLock`` is garbage-collected (as a dropped file object
    would be), or until the process dies. Release it in a ``finally`` all the same: that's
    the only way to be sure when it's freed. Two locks on the same file conflict even within
    one process (``flock`` locks belong to the open file, and Windows' byte-range locks to
    the handle), so a second window, thread or terminal can't take a lock the first one
    holds. On POSIX this is the same ``flock`` Tomecast has always used, so it also conflicts
    with an older Tomecast's lock on the same file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """Whether this object holds the lock now."""
        return self._fd is not None

    def acquire(self) -> bool:
        """Take the lock if it's free. Returns ``False`` at once if someone else holds it.

        Raises:
            RuntimeError: if this object already holds it.
            OSError: if the lock file can't be opened or locked for another reason.
        """
        if self._fd is not None:
            raise RuntimeError(f"{self.path} is already locked by this FileLock")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
        try:
            locked = _try_lock(fd)
        except BaseException:
            os.close(fd)
            raise
        if locked:
            self._fd = fd
        else:
            os.close(fd)
        return locked

    def release(self) -> None:
        """Let go of the lock (nothing happens if it isn't held)."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __del__(self) -> None:
        # A lock dropped while held (say, an exception between acquire() and the caller's
        # `try`) is let go, instead of being held until the process exits.
        with contextlib.suppress(Exception):
            self.release()


if sys.platform == "win32":

    def _try_lock(fd: int) -> bool:
        # msvcrt locks bytes from the current position. Byte 0 can be locked even in an
        # empty file (Windows allows locking past the end).
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except PermissionError:  # EACCES: another handle holds it
            return False
        return True

    def _unlock(fd: int) -> None:
        # Windows asks for locks to be released before the file is closed.
        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


# --- Child processes that stop with Tomecast --------------------------------------------------


def start_tied(args: Sequence[str], **popen_kwargs: Any) -> subprocess.Popen[Any]:
    """``subprocess.Popen(args, **popen_kwargs)``, for a child that must stop if Tomecast
    dies (crash, ``kill -9``, Task Manager), so a voice engine is never left running alone.

    Linux: a ``preexec_fn`` sets ``PR_SET_PDEATHSIG`` to SIGTERM. Linux sends it when the
    *thread* that started the child exits, so start long-lived children from a thread that
    lives as long as they should. ``prctl`` is looked up here, in the parent: loading a
    library in the forked child could deadlock if Tomecast has threads.

    Windows: the child starts suspended and with no console window (``CREATE_SUSPENDED |
    CREATE_NO_WINDOW``), is put in a Job Object that kills every process in it when its last
    handle closes, and only then runs. Tomecast holds that handle until it exits, however it
    exits, so the child (and anything it starts) never runs outside the job and can't outlive
    Tomecast.

    Other POSIX systems: a plain ``Popen`` (they have no parent-death signal).

    Raises:
        ValueError: if ``popen_kwargs`` sets ``preexec_fn`` (this function sets its own).
        OSError: if the child can't be started or, on Windows, tied to Tomecast. A child
            that started but couldn't be tied is killed first, never left running.
    """
    if "preexec_fn" in popen_kwargs:
        raise ValueError("start_tied() sets preexec_fn itself")
    if sys.platform == "win32":
        flags = popen_kwargs.pop("creationflags", 0)
        flags |= _CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(args, creationflags=flags, **popen_kwargs)
        try:
            _assign_to_job(process.pid)
            _resume(process.pid)
        except BaseException:
            process.kill()
            process.wait()
            raise
        return process
    if sys.platform == "linux":
        prctl = _linux_prctl()

        def stop_with_parent() -> None:  # runs in the child, after fork: only calls prctl
            prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)

        return subprocess.Popen(args, preexec_fn=stop_with_parent, **popen_kwargs)
    return subprocess.Popen(args, **popen_kwargs)


_PR_SET_PDEATHSIG = 1


@functools.cache
def _linux_prctl() -> Any:
    return ctypes.CDLL(None, use_errno=True).prctl


# The Windows structures (winnt.h, tlhelp32.h), defined on every platform so their layout can
# be tested anywhere. Sizes on 64-bit Windows: 64, 48, 144 and 28 bytes.
class JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),  # LARGE_INTEGER
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),  # DWORD
        ("MinimumWorkingSetSize", ctypes.c_size_t),  # SIZE_T
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),  # ULONG_PTR
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    )


class IoCounters(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", JobObjectBasicLimitInformation),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


class ThreadEntry32(ctypes.Structure):
    _fields_ = (
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),  # LONG
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    )


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JOBOBJECTINFOCLASS
_CREATE_SUSPENDED = 0x00000004  # not in the subprocess module
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_RESUME_FAILED = 0xFFFFFFFF  # ResumeThread's (DWORD)-1

_job_lock = threading.Lock()
_job_handle: int | None = None  # never closed: Windows closes it when Tomecast exits

if sys.platform == "win32":

    @functools.cache
    def _kernel32() -> ctypes.WinDLL:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, dword, boolean = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
        prototypes: dict[str, tuple[Any, tuple[Any, ...]]] = {
            "CreateJobObjectW": (handle, (ctypes.c_void_p, wintypes.LPCWSTR)),
            "SetInformationJobObject": (boolean, (handle, ctypes.c_int, ctypes.c_void_p, dword)),
            "OpenProcess": (handle, (dword, boolean, dword)),
            "AssignProcessToJobObject": (boolean, (handle, handle)),
            "CreateToolhelp32Snapshot": (handle, (dword, dword)),
            "Thread32First": (boolean, (handle, ctypes.c_void_p)),
            "Thread32Next": (boolean, (handle, ctypes.c_void_p)),
            "OpenThread": (handle, (dword, boolean, dword)),
            "ResumeThread": (dword, (handle,)),
            "CloseHandle": (boolean, (handle,)),
        }
        for name, (restype, argtypes) in prototypes.items():
            function = getattr(kernel32, name)
            function.restype, function.argtypes = restype, argtypes
        return kernel32

    def _kill_on_close_job() -> int:
        """This process's job for its children, made the first time it's needed."""
        global _job_handle
        kernel32 = _kernel32()
        with _job_lock:
            if _job_handle is None:
                job = kernel32.CreateJobObjectW(None, None)
                if not job:
                    raise ctypes.WinError(ctypes.get_last_error())
                info = JobObjectExtendedLimitInformation()
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not kernel32.SetInformationJobObject(
                    job,
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                ):
                    error = ctypes.get_last_error()
                    kernel32.CloseHandle(job)
                    raise ctypes.WinError(error)
                _job_handle = int(job)
            return _job_handle

    def _assign_to_job(pid: int) -> None:
        kernel32 = _kernel32()
        job = _kill_on_close_job()
        # Popen keeps its own handle open, so the PID can't be reused while we do this.
        process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(job, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process)

    def _resume(pid: int) -> None:
        """Let a process started with ``CREATE_SUSPENDED`` run: resume its (one) thread.
        Popen closes the thread handle CreateProcess returns, so the thread is found by
        listing the system's threads."""
        kernel32 = _kernel32()
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if snapshot is None or snapshot == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        resumed = 0
        try:
            entry = ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == pid:
                    thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if kernel32.ResumeThread(thread) == _RESUME_FAILED:
                            raise ctypes.WinError(ctypes.get_last_error())
                    finally:
                        kernel32.CloseHandle(thread)
                    resumed += 1
                found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        if not resumed:
            raise OSError(f"couldn't find process {pid}'s thread to start it")


# --- Stop signals ---------------------------------------------------------------------------

SignalHandler = Callable[[int, FrameType | None], object]


def stop_signals() -> tuple[signal.Signals, ...]:
    """The signals that ask Tomecast to stop tidily (save work, stop the engine).

    POSIX: SIGTERM (``kill``, logging out) and SIGINT (Ctrl-C). Windows: SIGINT (Ctrl-C) and
    SIGBREAK (Ctrl-Break, and closing the console window). Nothing on Windows sends a SIGTERM
    a handler can see: ``os.kill(pid, SIGTERM)`` there ends the process at once. When the
    window is open, Windows' log-off message reaches Qt instead (``aboutToQuit``).
    """
    if sys.platform == "win32":
        return (signal.SIGINT, signal.SIGBREAK)
    return (signal.SIGTERM, signal.SIGINT)


@contextlib.contextmanager
def on_signals(signals: Iterable[signal.Signals], handler: SignalHandler) -> Iterator[None]:
    """Run ``handler`` on each of ``signals`` inside the ``with`` block, then put the previous
    handlers back. Call it from the main thread (Python only sets handlers there)."""
    previous: dict[signal.Signals, Any] = {}
    try:
        for sig in signals:
            previous[sig] = signal.signal(sig, handler)
        yield
    finally:
        for sig, old in previous.items():
            # None means the old handler wasn't set from Python; the default is the best guess.
            signal.signal(sig, signal.SIG_DFL if old is None else old)
