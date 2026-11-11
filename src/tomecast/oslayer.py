"""What Tomecast does differently on Linux and on Windows, behind one set of functions.

Each function has a POSIX version and a Windows version, picked with ``sys.platform`` checks
that mypy understands, so ``mypy`` on Linux checks the Linux code and ``mypy --platform
win32`` (or mypy on a Windows runner) checks the Windows code. Nothing here is imported for
its side effects, and importing it loads nothing platform-specific beyond the standard
library.

* :class:`FileLock`: an exclusive lock on a lock file (``flock`` on POSIX, ``msvcrt.locking``
  on Windows), for the work-folder and install locks.
* :func:`child_options` and :func:`adopt_child`: stop a child process (the voice engine) if
  Tomecast itself dies (``PR_SET_PDEATHSIG`` on Linux, a Job Object that kills its processes
  when closed on Windows).
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
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path, PurePath
from types import FrameType
from typing import Any, TypedDict

if sys.platform == "win32":
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl

IS_WINDOWS = sys.platform == "win32"


# --- Folders --------------------------------------------------------------------------------


def data_dir() -> Path:
    """Tomecast's data folder (engines, models, work folders, ``installed.json``).

    Linux: ``$XDG_DATA_HOME/tomecast``, or ``~/.local/share/tomecast``. Windows:
    ``%LOCALAPPDATA%\\Tomecast``, or ``~\\AppData\\Local\\Tomecast`` if that isn't set.
    """
    if sys.platform == "win32":
        return Path(local_app_data(os.environ, Path.home())) / "Tomecast"
    return Path(xdg_data_home(os.environ, Path.home())) / "tomecast"


def config_dir() -> Path:
    """Where the window's settings live.

    Linux: ``$XDG_CONFIG_HOME/Tomecast``, or ``~/.config/Tomecast``: the folder ``QSettings``
    already uses (organization "Tomecast"), so nothing moves. Windows:
    ``%LOCALAPPDATA%\\Tomecast\\config``. ``QSettings`` uses the registry on Windows unless
    it's told to use an INI file; which one Tomecast uses there is still to be decided (#39).
    """
    if sys.platform == "win32":
        return Path(local_app_data(os.environ, Path.home())) / "Tomecast" / "config"
    return Path(xdg_config_home(os.environ, Path.home())) / "Tomecast"


def xdg_data_home(environ: Mapping[str, str], home: PurePath) -> PurePath:
    """``$XDG_DATA_HOME``, or ``~/.local/share``.

    Any non-empty value is used, even a relative one: that's what Tomecast has always done
    for its data folder, and changing it would move someone's downloads.
    """
    configured = environ.get("XDG_DATA_HOME")
    return type(home)(configured) if configured else home / ".local" / "share"


def xdg_config_home(environ: Mapping[str, str], home: PurePath) -> PurePath:
    """``$XDG_CONFIG_HOME`` if it's an absolute path (as the XDG spec and Qt require), or
    ``~/.config``."""
    configured = environ.get("XDG_CONFIG_HOME")
    if configured and type(home)(configured).is_absolute():
        return type(home)(configured)
    return home / ".config"


def local_app_data(environ: Mapping[str, str], home: PurePath) -> PurePath:
    """``%LOCALAPPDATA%`` if it's an absolute path, or ``~\\AppData\\Local``."""
    configured = environ.get("LOCALAPPDATA")
    if configured and type(home)(configured).is_absolute():
        return type(home)(configured)
    return home / "AppData" / "Local"


# --- File locks -----------------------------------------------------------------------------


class FileLock:
    """An exclusive lock on a lock file, taken without waiting.

    The file is created if it's missing and never truncated. The lock lasts until
    :meth:`release`; if the process dies, the system lets go of it. Two locks on the same
    file conflict even within one process (``flock`` locks belong to the open file, and
    Windows' byte-range locks to the handle), so a second window, thread or terminal can't
    take a lock the first one holds. On POSIX this is the same ``flock`` Tomecast has always
    used, so it also conflicts with an older Tomecast's lock on the same file.
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


class ChildOptions(TypedDict):
    """Keyword arguments for :class:`subprocess.Popen` that tie a child's life to Tomecast's.

    Use them with :func:`adopt_child` right after the child starts:

        process = subprocess.Popen(command, **child_options())
        adopt_child(process)
    """

    preexec_fn: Callable[[], object] | None
    creationflags: int


def child_options() -> ChildOptions:
    """Popen options that stop the child if Tomecast dies (crash, ``kill -9``, power-off of
    the terminal), so a voice engine is never left running on its own.

    Linux: a ``preexec_fn`` that sets ``PR_SET_PDEATHSIG`` to SIGTERM. Linux sends it when
    the *thread* that started the child exits, so start long-lived children from a thread
    that lives as long as they should. ``prctl`` is looked up here, in the parent: loading a
    library in the forked child could deadlock if Tomecast has threads.

    Windows: no console window for the child (``CREATE_NO_WINDOW``, so none flashes up when
    the window starts the engine). The tie itself is made by :func:`adopt_child`.

    Other POSIX systems: nothing (they have no parent-death signal).
    """
    if sys.platform == "win32":
        return {"preexec_fn": None, "creationflags": subprocess.CREATE_NO_WINDOW}
    if sys.platform == "linux":
        prctl = _linux_prctl()

        def stop_with_parent() -> None:  # runs in the child, after fork: only calls prctl
            prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)

        return {"preexec_fn": stop_with_parent, "creationflags": 0}
    return {"preexec_fn": None, "creationflags": 0}


def adopt_child(process: subprocess.Popen[Any]) -> None:
    """Finish tying ``process`` (started with :func:`child_options`) to Tomecast's life.

    Windows: puts it in a Job Object that kills every process in it when its last handle
    closes. Tomecast holds that handle until it exits, however it exits, so the child
    can't outlive it. POSIX: nothing more to do.

    Raises:
        OSError: on Windows, if the child is still running but couldn't be put in the job.
    """
    if sys.platform == "win32":
        _assign_to_job(process)


_PR_SET_PDEATHSIG = 1


@functools.cache
def _linux_prctl() -> Any:
    return ctypes.CDLL(None, use_errno=True).prctl


# The Job Object structures (winnt.h), defined on every platform so their layout can be
# tested anywhere. Sizes on 64-bit Windows: 64, 48 and 144 bytes.
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


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JOBOBJECTINFOCLASS
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100

_job_lock = threading.Lock()
_job_handle: int | None = None  # never closed: Windows closes it when Tomecast exits

if sys.platform == "win32":

    def _kernel32() -> ctypes.WinDLL:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    def _kill_on_close_job(kernel32: ctypes.WinDLL) -> int:
        """This process's job for its children, made the first time it's needed."""
        global _job_handle
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

    def _assign_to_job(process: subprocess.Popen[Any]) -> None:
        kernel32 = _kernel32()
        job = _kill_on_close_job(kernel32)
        # Popen keeps its own handle open, so the PID can't be reused while we do this.
        handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid)
        if not handle:
            error = ctypes.get_last_error()
            if process.poll() is not None:
                return  # it has already exited: nothing to stop
            raise ctypes.WinError(error)
        try:
            if not kernel32.AssignProcessToJobObject(job, handle):
                error = ctypes.get_last_error()
                if process.poll() is None:
                    raise ctypes.WinError(error)
        finally:
            kernel32.CloseHandle(handle)


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
