"""Fake ``/proc/meminfo`` files and ``/sys/class/power_supply`` folders for the hardware scan."""

from collections.abc import Mapping
from pathlib import Path

import pytest

from tomecast import machine

GIB_IN_KIB = 1024 * 1024

# Each entry as the kernel lists it: the folder name, then its attribute files.
AC_ONLINE = {"AC": {"type": "Mains", "online": "1"}}
AC_OFFLINE = {"AC": {"type": "Mains", "online": "0"}}
DISCHARGING = {"BAT0": {"type": "Battery", "present": "1", "status": "Discharging"}}
CHARGING = {"BAT0": {"type": "Battery", "present": "1", "status": "Charging"}}
ON_BATTERY = {**AC_OFFLINE, **DISCHARGING}
PLUGGED_IN = {**AC_ONLINE, **CHARGING}


def meminfo(path: Path, *, total_gib: float | None, available_gib: float | None) -> Path:
    """A ``/proc/meminfo`` with these values (``None`` leaves the line out)."""
    lines = []
    if total_gib is not None:
        lines.append(f"MemTotal:       {round(total_gib * GIB_IN_KIB)} kB")
    lines.append("MemFree:         1000000 kB")
    if available_gib is not None:
        lines.append(f"MemAvailable:   {round(available_gib * GIB_IN_KIB)} kB")
    lines.append("HugePages_Total:       0")
    path.write_text("\n".join(lines) + "\n")
    return path


def power_supply(folder: Path, supplies: Mapping[str, Mapping[str, str]]) -> Path:
    """A ``/sys/class/power_supply`` folder with these entries."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, attributes in supplies.items():
        entry = folder / name
        entry.mkdir()
        for attribute, value in attributes.items():
            (entry / attribute).write_text(f"{value}\n")
    return folder


def pin_machine(
    monkeypatch: pytest.MonkeyPatch,
    folder: Path,
    *,
    total_gib: float | None = 16,
    available_gib: float | None = 12,
    supplies: Mapping[str, Mapping[str, str]] = PLUGGED_IN,
) -> None:
    """Make the hardware scan read a fake machine built in ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        machine,
        "MEMINFO",
        meminfo(folder / "meminfo", total_gib=total_gib, available_gib=available_gib),
    )
    monkeypatch.setattr(machine, "POWER_SUPPLY", power_supply(folder / "power_supply", supplies))
