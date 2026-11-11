"""The window quits cleanly on SIGTERM and Ctrl-C (Qt's event loop would otherwise hide them)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _environment(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        QT_QPA_PLATFORMTHEME="",
        XDG_CONFIG_HOME=str(tmp_path / "config"),  # keep the window's settings out of ~
        XDG_DATA_HOME=str(tmp_path / "data"),
        TOMECAST_REDUCE_MOTION="1",
    )
    return env


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_window_quits_on_signal(tmp_path: Path, sig: signal.Signals) -> None:
    env = _environment(tmp_path)
    ready = tmp_path / "ready"
    # Start the real window in a child process and tell the test once its event loop runs.
    code = (
        "import sys, pathlib\n"
        "from PySide6.QtCore import QTimer\n"
        "from tomecast.gui import app\n"
        f"QTimer.singleShot(0, lambda: pathlib.Path({str(ready)!r}).touch())\n"
        "sys.exit(app.run([]))\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert child.poll() is None, "the window exited before it was ready"
            assert time.monotonic() < deadline, "the window never started"
            time.sleep(0.05)
        child.send_signal(sig)
        assert child.wait(timeout=20) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_as_the_window_starts_quits_cleanly(tmp_path: Path, sig: signal.Signals) -> None:
    """Ctrl-C (or `kill`) just after `tomecast gui` starts: the worker thread is running, and
    neither the window nor its event loop is there yet. It quits as soon as the loop runs,
    instead of leaving the thread for Qt to destroy (SIGABRT and a core dump)."""
    code = (
        "import ctypes, os, sys\n"
        "ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0: no core dump if it aborts\n"
        "from tomecast import cli\n"
        "from tomecast.gui import app\n"
        "made = app._register_dot_grid  # called just after the backend is made\n"
        "def signalled():\n"
        f"    os.kill(os.getpid(), {int(sig)})\n"
        "    made()\n"
        "app._register_dot_grid = signalled\n"
        "sys.exit(cli.main(['gui']))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr  # not 130 and then SIGABRT, and not left running
    assert "Destroyed while thread" not in done.stderr
    assert "KeyboardInterrupt" not in done.stderr
