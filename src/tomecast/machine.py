"""The rest of the hardware scan: CPU threads, memory and power, and the setting it picks.

The graphics card is found by :mod:`tomecast.hardware`. Everything here is a plain file read
from ``/proc`` and ``/sys`` (Linux only, no dependencies), cheap enough to do again each time
an engine starts, so unplugging the charger counts from the next start. Nothing here raises: a
file that can't be read means "unknown", and an unknown never makes the voice use fewer
threads.

* :func:`scan_machine` reads the CPU count, the memory and the power state into a
  :class:`Machine`.
* :func:`recommend` turns that into a :class:`Recommendation`: the voice and its thread count.
  Kokoro is the only voice for now; recommending Ming on a graphics card with 4 GB or more
  (docs/architecture.md, section 5) is added there with issue #11.
* :func:`memory_note` warns when too little memory is free to voice comfortably.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Where the scan looks. Read when called, so tests can point them at a fake tree.
MEMINFO = Path("/proc/meminfo")
POWER_SUPPLY = Path("/sys/class/power_supply")

# audio.cpp got slower with every hyperthread in use, and gains little past this many threads.
MAX_THREADS = 8
# The voice engine's peak memory while voicing a full 200-character chunk: audio.cpp 0.9.4's CPU
# build with Kokoro Q8_0 measured 1.8 GB at 1, 2, 4 and 8 threads alike (330 MB idle), so fewer
# threads don't save memory; they leave the rest of a small machine room to work.
VOICE_MEMORY_BYTES = 1_800_000_000
# Less memory than this in total makes a low-memory machine: in practice one with 4 GB (which
# reports about 3.7 GiB) or 8 GB lending 2 GB or more to built-in graphics. The voice alone
# takes a third of it or more, so it voices with fewer threads, as on battery.
LOW_MEMORY_BYTES = 6 * 2**30
# Less free memory than this before voicing gets a warning: the voice's peak, plus about 200 MB
# for packaging with ffmpeg and for the system, which otherwise starts swapping.
MIN_FREE_BYTES = 2_000_000_000

# Outside power supplies (the charger, USB-C, wireless charging). "UPS" isn't one: a desktop on
# a UPS isn't a laptop, so a UPS never counts either way.
_CHARGERS = ("Mains", "USB", "Wireless", "BrickID")

ON_BATTERY = "on battery"
LOW_MEMORY = "low memory"


@dataclass(frozen=True, slots=True)
class Memory:
    """Total and available memory in bytes, from ``/proc/meminfo``; ``None`` if unknown."""

    total_bytes: int | None = None
    available_bytes: int | None = None  # what can be used without swapping (MemAvailable)


@dataclass(frozen=True, slots=True)
class Power:
    """Whether the computer has its own battery, and runs on it now; ``None`` if unknown."""

    battery: bool | None = None  # a laptop (a battery of the system's, not a mouse's or a UPS)
    on_battery: bool | None = None  # the battery is powering it now (no charger)


@dataclass(frozen=True, slots=True)
class Machine:
    """What the scan found, besides the graphics card (see :mod:`tomecast.hardware`)."""

    cpus: int  # logical CPUs Tomecast may use
    memory: Memory
    power: Power

    @property
    def low_memory(self) -> bool:
        total = self.memory.total_bytes
        return total is not None and total < LOW_MEMORY_BYTES


@dataclass(frozen=True, slots=True)
class Recommendation:
    """The setting the scan picks (docs/architecture.md, section 5)."""

    voice: Literal["kokoro"]  # the only one for now; Ming comes with issue #11
    threads: int  # CPU threads the voice engine is told to use
    usual_threads: int  # what it would use plugged in, with enough memory
    fewer_because: tuple[str, ...] = ()  # ON_BATTERY and/or LOW_MEMORY, when threads < usual


def scan_machine(*, meminfo: Path | None = None, power_supply: Path | None = None) -> Machine:
    """The CPU count, memory and power state now. Never raises."""
    return Machine(cpu_count(), read_memory(meminfo), read_power(power_supply))


def cpu_count() -> int:
    """The logical CPUs this process may run on (2 if that can't be told)."""
    try:
        return len(os.sched_getaffinity(0)) or 2
    except (AttributeError, OSError):
        return os.cpu_count() or 2


def read_memory(meminfo: Path | None = None) -> Memory:
    """MemTotal and MemAvailable from ``/proc/meminfo``. Never raises."""
    try:
        text = (meminfo or MEMINFO).read_text(errors="replace")
    except (OSError, ValueError):
        return Memory()
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[name.strip()] = int(fields[0]) * (1024 if fields[1:] == ["kB"] else 1)
    return Memory(values.get("MemTotal") or None, values.get("MemAvailable"))


def read_power(power_supply: Path | None = None) -> Power:
    """Whether this is a laptop, and on battery now, from ``/sys/class/power_supply``.

    Only the system's own batteries count: one whose ``scope`` is ``Device`` (a mouse,
    keyboard, headset or game controller) is left out, and so is one with no ``scope`` that is
    plainly an input device's (see :func:`_peripheral`), and a ``UPS``. No battery means a
    desktop. A laptop is plugged in if a charger (``Mains``, ``USB``, ``Wireless``) is online
    or a battery is charging; otherwise it's on battery if a battery is discharging or every
    charger says it's offline. Never raises.
    """
    folder = power_supply or POWER_SUPPLY
    try:
        entries = sorted(folder.iterdir())
    except (OSError, ValueError):
        return Power()
    statuses: list[str] = []  # of the system's batteries
    charger_online: bool | None = None  # True if any charger is online, False if all are off
    for entry in entries:
        kind = _attribute(entry, "type")
        scope = _attribute(entry, "scope")
        if kind is None or scope == "Device":
            continue
        if kind == "Battery" and scope is None and _peripheral(entry):
            continue
        if kind == "Battery":
            if _attribute(entry, "present") != "0":
                statuses.append(_attribute(entry, "status") or "Unknown")
        elif kind.startswith(_CHARGERS):
            online = _attribute(entry, "online")  # 0 offline; 1 online, or 2 for USB PD
            if online is not None and online.isdigit():
                charger_online = charger_online or int(online) > 0
    if not statuses:
        return Power(battery=False, on_battery=False)
    on_battery: bool | None
    if charger_online or "Charging" in statuses:
        # A charging battery is plugged in, whatever an AC entry says: some laptops' AC entry
        # only follows the barrel jack while they charge over USB-C, and the two updates can
        # arrive a moment apart.
        on_battery = False
    elif "Discharging" in statuses or charger_online is False:
        on_battery = True
    elif "Full" in statuses:
        on_battery = False
    else:
        on_battery = None  # e.g. only "Unknown" or "Not charging", and no charger listed
    return Power(battery=True, on_battery=on_battery)


def _peripheral(entry: Path) -> bool:
    """A battery with no ``scope`` that is still an input device's: named by an input driver
    (``hid-…-battery``, ``hidpp_battery_0``), or belonging to a device on the HID bus (a USB
    or Bluetooth mouse, keyboard or controller). A laptop's own battery is an ACPI or platform
    device."""
    if entry.name.lower().startswith("hid"):
        return True
    try:
        return os.path.basename(os.readlink(entry / "device" / "subsystem")) == "hid"
    except (OSError, ValueError):
        return False


def _attribute(entry: Path, name: str) -> str | None:
    try:
        return (entry / name).read_text(errors="replace").strip()
    except (OSError, ValueError):
        return None


def usual_threads(cpus: int) -> int:
    """Physical cores, roughly (half the logical CPUs), from 1 to :data:`MAX_THREADS`."""
    return max(1, min(MAX_THREADS, cpus // 2))


def recommend(machine: Machine) -> Recommendation:
    """The voice and its thread count for ``machine``.

    The usual count is :func:`usual_threads`. On battery, or with less than
    :data:`LOW_MEMORY_BYTES` in total ("Old or low-RAM laptop: Kokoro, fewer threads"), it's
    half that, rounded up: 8 becomes 4 and 2 becomes 1. Both together still halve it once.
    """
    usual = usual_threads(machine.cpus)
    reasons: tuple[str, ...] = (ON_BATTERY,) if machine.power.on_battery is True else ()
    if machine.low_memory:
        reasons += (LOW_MEMORY,)
    threads = (usual + 1) // 2 if reasons else usual
    return Recommendation("kokoro", threads, usual, reasons if threads < usual else ())


def voice_threads() -> int:
    """The thread count the scan picks for this computer now."""
    return recommend(scan_machine()).threads


def memory_note(machine: Machine) -> str | None:
    """A one-sentence warning if less than :data:`MIN_FREE_BYTES` is free, else ``None``."""
    free = machine.memory.available_bytes
    if free is None or free >= MIN_FREE_BYTES:
        return None
    return (
        f"Only {free / 1e9:.1f} GB of memory is free, and the voice needs about "
        f"{VOICE_MEMORY_BYTES / 1e9:.1f} GB while it works, so this computer may slow down a "
        "lot. Closing other programs frees memory."
    )
