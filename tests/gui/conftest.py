"""Run every GUI test headless, on the same machine whatever runs it, and never leave a
window's worker thread running.

The environment is assigned, not defaulted: a desktop session sets QT_QPA_PLATFORM (for
example to "wayland;xcb"), and the tests would then open real windows.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tomecast import machine

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
# The desktop's Qt theme plugin could otherwise leak its colour scheme into the tests.
os.environ["QT_QPA_PLATFORMTHEME"] = ""

# How long a test's leftover worker may take to finish after shutdown() (which is bounded,
# and a test may leave a fake engine blocked for a while).
WORKER_STOP_MS = 60_000
# Workers that wouldn't stop even so: kept referenced, so Qt never destroys a running thread
# (which aborts the whole test run with a core dump), and reported as a failure instead.
_STUCK: list[Any] = []


@pytest.fixture(autouse=True)
def every_backend_is_shut_down(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Shut down every ``Backend`` a test makes, and wait for its worker thread to finish,
    whatever the test does (a test that fails part-way never reaches its own shutdown()).
    Otherwise the backend is freed later, by the garbage collector or with an old traceback,
    while its thread still runs: "QThread: Destroyed while thread 'tomecast-worker' is still
    running", then SIGABRT and a core dump of the test run."""
    from tomecast.gui import backend as backend_module

    made: list[Any] = []
    original = backend_module.Backend.__init__

    def recording(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        made.append(self)

    monkeypatch.setattr(backend_module.Backend, "__init__", recording)
    yield
    stuck = []
    for backend in made:
        backend.shutdown()
        if not backend.worker_thread.wait(WORKER_STOP_MS):
            _STUCK.append(backend)
            stuck.append(backend)
    if stuck:
        pytest.fail(f"{len(stuck)} backend worker(s) still running after shutdown()")


@pytest.fixture(autouse=True)
def _unknown_memory_and_power(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hardware scan finds nothing to read, so it never picks fewer threads: a laptop on
    battery would otherwise change the hardware label."""
    monkeypatch.setattr(machine, "MEMINFO", tmp_path / "no-meminfo")
    monkeypatch.setattr(machine, "POWER_SUPPLY", tmp_path / "no-power-supply")
