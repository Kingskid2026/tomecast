"""The window quits cleanly on SIGTERM and Ctrl-C (Qt's event loop would otherwise hide them)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_window_quits_on_signal(tmp_path: Path, sig: signal.Signals) -> None:
    env = dict(os.environ)
    env.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        QT_QPA_PLATFORMTHEME="",
        XDG_CONFIG_HOME=str(tmp_path / "config"),  # keep the window's settings out of ~
        XDG_DATA_HOME=str(tmp_path / "data"),
        TOMECAST_REDUCE_MOTION="1",
    )
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
