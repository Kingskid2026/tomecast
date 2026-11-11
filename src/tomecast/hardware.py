"""Finding a graphics card to voice with, using the driver the system already has.

Tomecast never installs, updates or configures a graphics driver. It asks two questions:

* :func:`vulkan_possible` is a cheap check with no download: is there a Vulkan loader and a
  graphics device node?
* :func:`list_devices` asks audio.cpp's own Vulkan build which devices the installed drivers
  expose, so Tomecast needs no detection code of its own.

:func:`pick_device` chooses among them. Nothing here raises on a broken driver: the answer is
"no graphics card" or "couldn't tell", and the caller uses the CPU.
"""

from __future__ import annotations

import contextlib
import ctypes
import glob
import json
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# An integrated GPU shares system memory, so it needs this much free to be worth using.
MIN_IGPU_FREE_BYTES = 2_000_000_000

_DEVICE_NODES = ("/dev/dri/renderD*", "/dev/nvidia0")


@dataclass(frozen=True, slots=True)
class GpuDevice:
    """One Vulkan device, as listed by ``audiocpp_server --list-devices``."""

    index: int  # the number the server's ``device`` setting takes
    name: str  # e.g. "Vulkan0"
    description: str  # e.g. "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU"
    kind: str  # "gpu" (discrete) or "igpu" (built in)
    free_bytes: int
    total_bytes: int


def vulkan_possible() -> bool:
    """True if a Vulkan loader can be loaded and a graphics device node exists.

    A cheap guess made before anything is downloaded; the real test is starting the engine.
    """
    try:
        ctypes.CDLL("libvulkan.so.1")
    except OSError:
        return False
    return any(glob.glob(pattern) for pattern in _DEVICE_NODES)


def list_devices(
    executable: Path | str,
    timeout: float = 20,
    cancelled: Callable[[], bool] | None = None,
) -> list[GpuDevice] | None:
    """The Vulkan devices ``executable`` (audio.cpp's Vulkan build) can see.

    ``[]`` means it answered and there's no device. ``None`` means it couldn't tell: the
    program is missing, crashed before printing the list, hung past ``timeout``, printed
    nothing usable, or ``cancelled`` said the user stopped. A card that doesn't answer isn't
    "no card", so callers must not treat ``None`` as one. Never raises. The server logs on
    stderr and prints one JSON object on stdout; the last stdout line that parses as JSON is
    used, even if the program then fails (a driver that crashes as it shuts down, after
    printing the list): the self-check and the engine's start catch a card that doesn't work.
    An empty list followed by a failure isn't an answer (``None``): a driver that failed to
    start up lists no cards.
    """
    try:
        process = subprocess.Popen(
            [str(executable), "--backend", "vulkan", "--list-devices", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, _ = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline or (cancelled is not None and cancelled()):
                    return None
    finally:
        # Out early (timeout, stop, Ctrl-C): kill it and reap it, but don't wait for the end of
        # stdout, which a child process it started may still hold open.
        if process.returncode is None:
            process.kill()
            with contextlib.suppress(subprocess.SubprocessError):
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
    payload: object = None
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        break
    devices = _parse_devices(payload)
    if process.returncode != 0 and not devices:
        # It failed, and said of no card: a driver that failed to start up lists nothing, so
        # that isn't an answer. Only a list of cards it printed before failing is one.
        return None
    return devices


def _parse_devices(payload: object) -> list[GpuDevice] | None:
    if not isinstance(payload, dict):
        return None
    entries = payload.get("devices")
    if not isinstance(entries, list):
        return None
    devices: list[GpuDevice] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("backend") != "vulkan":
            continue
        memory = entry.get("memory")
        memory = memory if isinstance(memory, dict) else {}
        index = entry.get("index")
        name = entry.get("name")
        if isinstance(index, bool) or not isinstance(index, int) or not isinstance(name, str):
            continue
        description = entry.get("description")
        devices.append(
            GpuDevice(
                index=index,
                name=name,
                description=description if isinstance(description, str) and description else name,
                kind=str(entry.get("type", "")),
                free_bytes=_count(memory.get("free_bytes")),
                total_bytes=_count(memory.get("total_bytes")),
            )
        )
    return devices


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def pick_device(devices: Sequence[GpuDevice]) -> GpuDevice | None:
    """The discrete GPU with the most free memory, else a built-in one with enough, else None."""
    discrete = [d for d in devices if d.kind == "gpu"]
    if discrete:
        return max(discrete, key=lambda d: d.free_bytes)
    built_in = [d for d in devices if d.kind == "igpu" and d.free_bytes >= MIN_IGPU_FREE_BYTES]
    if built_in:
        return max(built_in, key=lambda d: d.free_bytes)
    return None


# Words that say nothing about which card it is.
_NOISE = {
    "graphics", "gpu", "laptop", "generation", "mobile", "series", "edition", "blackwell",
    "ada", "lovelace", "ampere", "turing", "pascal",
}  # fmt: skip


def short_name(description: str) -> str:
    """A device description as a short label: "NVIDIA RTX PRO 1000 Blackwell Generation Laptop
    GPU" becomes "NVIDIA RTX PRO 1000"; "AMD Radeon 860M Graphics (RADV KRACKAN1)" becomes
    "AMD RADEON 860M". Falls back to the whole description if nothing would be left."""
    text = re.sub(r"\([^)]*\)", " ", description)
    words = [word for word in text.split() if word.lower() not in _NOISE]
    return " ".join(words).upper() if words else description.strip().upper()
