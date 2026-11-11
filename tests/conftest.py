"""Set-up shared by every test: the window runs headless, the hardware scan reads nothing
from this machine, and no GUI test leaves a window's worker thread running.

The Qt variables are assigned, not defaulted: a desktop session sets QT_QPA_PLATFORM (for
example to "wayland;xcb"), and the GUI tests would then open real windows. They live here
rather than in tests/gui so there is one conftest module (mypy checks tests/ as top-level
modules, and two files named conftest would collide).
"""

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tomecast import machine

if sys.platform == "win32":
    # The Windows port (#39) isn't done yet. These import tomecast.engines.audiocpp, which
    # calls ctypes.CDLL(None) (Linux's prctl) when it loads and so can't be imported on
    # Windows, or (test_cli, gui/test_backend) import fcntl themselves. The window's tests
    # all load tomecast.gui.backend (through the fixture below), which imports the engine.
    # Remove names from this list as the call sites move to tomecast.oslayer.
    collect_ignore_glob = [
        "gui/*",
        "test_audiocpp.py",
        "test_cli.py",
        "test_machine.py",
        "test_out_of_memory.py",
        "test_speed.py",
        "test_workdirs.py",
    ]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip ``posix_only`` tests on Windows (they're kept, not deleted, for the port)."""
    if sys.platform != "win32":
        return
    skip = pytest.mark.skip(reason="POSIX-only: not ported to Windows yet (#39)")
    for item in items:
        if item.get_closest_marker("posix_only"):
            item.add_marker(skip)


os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
# The desktop's Qt theme plugin could otherwise leak its colour scheme into the tests.
os.environ["QT_QPA_PLATFORMTHEME"] = ""

GUI_TESTS = Path(__file__).parent / "gui"
# How long a test's leftover worker may take to finish after shutdown() (which is bounded,
# and a test may leave a fake engine blocked for a while).
WORKER_STOP_MS = 60_000
# Workers that wouldn't stop even so: kept referenced, so Qt never destroys a running thread
# (which aborts the whole test run with a core dump), and reported as a failure instead.
_STUCK: list[Any] = []


@pytest.fixture(autouse=True)
def _unknown_memory_and_power(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan finds no /proc/meminfo or /sys/class/power_supply, so it never picks fewer
    threads or warns about memory: pulling the charger mid-run, or a busy machine, can't
    change a test. Tests of the scan itself point it at fake files instead."""
    monkeypatch.setattr(machine, "MEMINFO", tmp_path / "no-meminfo")
    monkeypatch.setattr(machine, "POWER_SUPPLY", tmp_path / "no-power-supply")


@pytest.fixture(autouse=True)
def every_backend_is_shut_down(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Shut down every ``Backend`` a GUI test makes, and wait for its worker thread to finish,
    whatever the test does (a test that fails part-way never reaches its own shutdown()).
    Otherwise the backend is freed later, by the garbage collector or with an old traceback,
    while its thread still runs: "QThread: Destroyed while thread 'tomecast-worker' is still
    running", then SIGABRT and a core dump of the test run. Other tests don't import Qt."""
    if not request.node.path.is_relative_to(GUI_TESTS):
        yield
        return
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
