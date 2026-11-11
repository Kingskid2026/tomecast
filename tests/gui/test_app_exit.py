"""Closing the window while the worker is stuck leaves cleanly, instead of aborting.

If the worker is still busy after the bounded waits (an engine stuck in a graphics driver for
minutes, a download read that stalled), returning from ``run()`` lets Qt destroy a running
QThread: "QThread: Destroyed while thread is still running", then SIGABRT and a core dump.
``app.finish()`` writes the settings and leaves with ``os._exit`` instead. That can only be
seen from outside, so this runs a real backend in a separate process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

SCRIPT = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication([])
    from tomecast.gui import app as app_module, backend as backend_module
    from tomecast.speed import Speeds

    backend_module.SHUTDOWN_WAIT_MS = 100
    started = []

    def stuck(root, gpu, cancelled, devices):  # an engine stuck in a graphics driver
        started.append(True)
        time.sleep(60)
        return Speeds()

    data = Path(sys.argv[1])
    backend = backend_module.Backend(
        gpu_check=lambda: False,
        device_scanner=lambda root, cancelled: None,
        speed_probe=stuck,
        assets=(),
        data_root=data,
        player=None,
        settings=QSettings(str(data / "settings.ini"), QSettings.Format.IniFormat),
        system_check=lambda: None,
    )
    while not started:
        time.sleep(0.05)
    backend._settings.setValue("voice", "bf_emma")  # changed just before closing, not written
    sys.exit(app_module.finish(backend, 7))
    """
)


def test_closing_with_a_stuck_worker_leaves_cleanly_and_keeps_the_settings(
    tmp_path: Path,
) -> None:
    script = tmp_path / "close_while_stuck.py"
    script.write_text(SCRIPT)
    data = tmp_path / "data"
    data.mkdir()
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QPA_PLATFORMTHEME": ""}
    started = time.monotonic()

    done = subprocess.run(
        [sys.executable, str(script), str(data)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )

    assert done.returncode == 7, done.stderr  # the window's own exit code, not 134 (SIGABRT)
    assert "Destroyed while thread" not in done.stderr
    assert time.monotonic() - started < 30  # it didn't wait for the stuck engine
    assert "voice=bf_emma" in (data / "settings.ini").read_text()  # written before leaving
