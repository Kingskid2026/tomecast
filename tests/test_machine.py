from collections.abc import Mapping
from pathlib import Path

import pytest
from machine_factory import (
    AC_OFFLINE,
    AC_ONLINE,
    CHARGING,
    DISCHARGING,
    ON_BATTERY,
    PLUGGED_IN,
    meminfo,
    pin_machine,
    power_supply,
)

from tomecast import machine
from tomecast.engines.audiocpp import default_threads
from tomecast.machine import (
    LOW_MEMORY,
    LOW_MEMORY_BYTES,
    MIN_FREE_BYTES,
    Machine,
    Memory,
    Power,
    Recommendation,
    memory_note,
    read_memory,
    read_power,
    recommend,
    scan_machine,
    usual_threads,
)

GIB = 2**30
DESKTOP = Power(battery=False, on_battery=False)
PLENTY = Memory(16 * GIB, 12 * GIB)


# --- Memory -----------------------------------------------------------------------------------


def test_reads_total_and_available_memory(tmp_path: Path) -> None:
    path = meminfo(tmp_path / "meminfo", total_gib=16, available_gib=9.5)

    assert read_memory(path) == Memory(16 * GIB, round(9.5 * GIB))


def test_reads_the_kernels_own_layout(tmp_path: Path) -> None:
    path = tmp_path / "meminfo"
    path.write_text(
        "MemTotal:       31998496 kB\n"
        "MemFree:         1270880 kB\n"
        "MemAvailable:    8466896 kB\n"
        "Buffers:            1332 kB\n"
        "HugePages_Total:       0\n"
        "Hugepagesize:       2048 kB\n"
    )

    assert read_memory(path) == Memory(31998496 * 1024, 8466896 * 1024)


def test_missing_lines_are_unknown(tmp_path: Path) -> None:
    path = meminfo(tmp_path / "meminfo", total_gib=None, available_gib=None)

    assert read_memory(path) == Memory(None, None)


@pytest.mark.parametrize("text", ["", "garbage\n", "MemTotal: lots kB\n", "MemTotal: 0 kB\n"])
def test_odd_meminfo_is_unknown(tmp_path: Path, text: str) -> None:
    path = tmp_path / "meminfo"
    path.write_text(text)

    assert read_memory(path).total_bytes is None


def test_an_unreadable_meminfo_is_unknown(tmp_path: Path) -> None:
    assert read_memory(tmp_path / "missing") == Memory()
    assert read_memory(tmp_path) == Memory()  # a folder, not a file


# --- Power ------------------------------------------------------------------------------------


def _power(tmp_path: Path, supplies: Mapping[str, Mapping[str, str]]) -> Power:
    return read_power(power_supply(tmp_path / "power_supply", supplies))


def test_no_power_supplies_is_a_desktop(tmp_path: Path) -> None:
    assert _power(tmp_path, {}) == DESKTOP


def test_a_laptop_on_its_charger(tmp_path: Path) -> None:
    assert _power(tmp_path, PLUGGED_IN) == Power(battery=True, on_battery=False)


def test_a_laptop_on_battery(tmp_path: Path) -> None:
    assert _power(tmp_path, ON_BATTERY) == Power(battery=True, on_battery=True)


def test_a_full_battery_on_the_charger_isnt_on_battery(tmp_path: Path) -> None:
    # As the maintainer's laptop reports it: charging stopped at a limit, the charger plugged
    # in, and an ACPI battery with no scope file.
    supplies = {
        "AC": {"type": "Mains", "online": "1"},
        "BAT0": {"type": "Battery", "present": "1", "status": "Not charging", "capacity": "100"},
        "ucsi-source-psy-USBC000:001": {"type": "USB", "scope": "System", "online": "0"},
        "ucsi-source-psy-USBC000:002": {"type": "USB", "scope": "System", "online": "1"},
    }

    assert _power(tmp_path, supplies) == Power(battery=True, on_battery=False)


def test_a_usb_c_charger_counts_as_plugged_in(tmp_path: Path) -> None:
    supplies = {
        **DISCHARGING,  # a weak charger: the battery still drains a little
        "ucsi-source-psy-USBC000:001": {"type": "USB", "scope": "System", "online": "1"},
    }

    assert _power(tmp_path, supplies).on_battery is False


def test_a_programmable_usb_supply_counts_as_online(tmp_path: Path) -> None:
    supplies = {**DISCHARGING, "tcpm-source-psy-1-0022": {"type": "USB", "online": "2"}}

    assert _power(tmp_path, supplies).on_battery is False


def test_unplugged_usb_c_ports_mean_on_battery(tmp_path: Path) -> None:
    supplies = {
        "BAT0": {"type": "Battery", "status": "Unknown"},
        "ucsi-source-psy-USBC000:001": {"type": "USB", "scope": "System", "online": "0"},
    }

    assert _power(tmp_path, supplies).on_battery is True


def test_a_charger_that_says_offline_means_on_battery_whatever_the_battery_says(
    tmp_path: Path,
) -> None:
    supplies = {**AC_OFFLINE, "BAT0": {"type": "Battery", "status": "Unknown"}}

    assert _power(tmp_path, supplies) == Power(battery=True, on_battery=True)


@pytest.mark.parametrize(("status", "on_battery"), [
    ("Discharging", True), ("Charging", False), ("Full", False),
    ("Not charging", None), ("Unknown", None),
])  # fmt: skip
def test_without_a_charger_listed_the_battery_status_decides(
    tmp_path: Path, status: str, on_battery: bool | None
) -> None:
    supplies = {"BAT1": {"type": "Battery", "status": status}}

    assert _power(tmp_path, supplies) == Power(battery=True, on_battery=on_battery)


def test_a_mouse_battery_doesnt_make_a_desktop_a_laptop(tmp_path: Path) -> None:
    supplies = {
        "hidpp_battery_0": {
            "type": "Battery", "scope": "Device", "status": "Discharging", "online": "1",
        },
        "ps-controller-battery-aa:bb:cc:dd:ee:ff": {
            "type": "Battery", "scope": "Device", "status": "Discharging",
        },
    }  # fmt: skip

    assert _power(tmp_path, supplies) == DESKTOP


def test_a_mouse_battery_doesnt_hide_the_laptops_own(tmp_path: Path) -> None:
    supplies = {
        **ON_BATTERY,
        "hid-00:11:22:33:44:55-battery": {
            "type": "Battery", "scope": "Device", "status": "Charging",
        },
        "usb-headset": {"type": "USB", "scope": "Device", "online": "1"},
    }  # fmt: skip

    assert _power(tmp_path, supplies) == Power(battery=True, on_battery=True)


def test_a_ups_on_battery_doesnt_count(tmp_path: Path) -> None:
    supplies = {"ups0": {"type": "UPS", "online": "0", "status": "Discharging"}}

    assert _power(tmp_path, supplies) == DESKTOP


def test_a_ups_doesnt_change_a_laptops_answer(tmp_path: Path) -> None:
    ups_online = {"ups0": {"type": "UPS", "online": "1", "status": "Charging"}}

    assert _power(tmp_path / "a", {**ON_BATTERY, **ups_online}).on_battery is True
    ups_offline = {"ups0": {"type": "UPS", "online": "0", "status": "Discharging"}}
    assert _power(tmp_path / "b", {**PLUGGED_IN, **ups_offline}).on_battery is False


def test_an_empty_battery_bay_doesnt_count(tmp_path: Path) -> None:
    supplies = {**AC_OFFLINE, "BAT0": {"type": "Battery", "present": "0", "status": "Unknown"}}

    assert _power(tmp_path, supplies) == DESKTOP


def test_any_charger_online_wins(tmp_path: Path) -> None:
    supplies = {
        **CHARGING,
        "ucsi-source-psy-USBC000:001": {"type": "USB", "online": "0"},
        **AC_ONLINE,
        "ucsi-source-psy-USBC000:002": {"type": "USB", "online": "0"},
    }

    assert _power(tmp_path, supplies).on_battery is False


def test_entries_that_cant_be_read_are_skipped(tmp_path: Path) -> None:
    folder = power_supply(tmp_path / "power_supply", ON_BATTERY)
    (folder / "broken").mkdir()  # no type file
    (folder / "odd").mkdir()
    (folder / "odd" / "type").mkdir()  # a folder where a file should be
    (folder / "stray-file").write_text("Battery\n")

    assert read_power(folder) == Power(battery=True, on_battery=True)


def test_an_unreadable_power_supply_folder_is_unknown(tmp_path: Path) -> None:
    assert read_power(tmp_path / "missing") == Power(battery=None, on_battery=None)
    (tmp_path / "file").write_text("")
    assert read_power(tmp_path / "file") == Power()


# --- The thread count ---------------------------------------------------------------------------


@pytest.mark.parametrize(("cpus", "threads"), [
    (1, 1), (2, 1), (4, 2), (8, 4), (12, 6), (16, 8), (64, 8),
])  # fmt: skip
def test_the_usual_count_is_the_physical_cores_up_to_eight(cpus: int, threads: int) -> None:
    assert usual_threads(cpus) == threads


def test_plugged_in_with_plenty_of_memory_uses_the_usual_count() -> None:
    assert recommend(Machine(16, PLENTY, Power(True, False))) == Recommendation("kokoro", 8, 8)


def test_on_battery_halves_the_threads() -> None:
    choice = recommend(Machine(16, PLENTY, Power(True, True)))

    assert choice == Recommendation("kokoro", 4, 8, ("on battery",))


def test_little_memory_halves_the_threads() -> None:
    tight = Memory(round(3.7 * GIB), GIB)

    assert recommend(Machine(8, tight, DESKTOP)) == Recommendation("kokoro", 2, 4, (LOW_MEMORY,))


def test_on_battery_with_little_memory_halves_them_once() -> None:
    tight = Memory(4 * GIB, 2 * GIB)

    choice = recommend(Machine(16, tight, Power(True, True)))

    assert choice.threads == 4
    assert choice.fewer_because == ("on battery", LOW_MEMORY)


@pytest.mark.parametrize(("usual", "fewer"), [(1, 1), (2, 1), (3, 2), (4, 2), (5, 3), (8, 4)])
def test_fewer_is_half_rounded_up(usual: int, fewer: int) -> None:
    choice = recommend(Machine(usual * 2, PLENTY, Power(True, True)))

    assert (choice.usual_threads, choice.threads) == (usual, fewer)


def test_no_reason_is_given_when_one_thread_cant_be_fewer() -> None:
    assert recommend(Machine(2, PLENTY, Power(True, True))).fewer_because == ()


def test_eight_gigabytes_isnt_low_memory() -> None:
    eight = Memory(round(7.6 * GIB), 4 * GIB)  # what an 8 GB machine reports

    assert recommend(Machine(16, eight, DESKTOP)).threads == 8
    assert recommend(Machine(16, Memory(LOW_MEMORY_BYTES, GIB), DESKTOP)).threads == 8
    assert recommend(Machine(16, Memory(LOW_MEMORY_BYTES - 1, GIB), DESKTOP)).threads == 4


def test_unknowns_never_mean_fewer_threads() -> None:
    assert recommend(Machine(16, Memory(), Power())).threads == 8
    assert recommend(Machine(16, PLENTY, Power(True, None))).threads == 8


# --- The low-memory note ----------------------------------------------------------------------


def test_warns_when_less_than_two_gigabytes_are_free() -> None:
    note = memory_note(Machine(8, Memory(4 * GIB, 1_200_000_000), DESKTOP))

    assert note == (
        "Only 1.2 GB of memory is free, and the voice needs about 1.8 GB while it works, so "
        "this computer may slow down a lot. Closing other programs frees memory."
    )


def test_no_warning_with_enough_free_or_when_unknown() -> None:
    assert memory_note(Machine(8, Memory(4 * GIB, MIN_FREE_BYTES), DESKTOP)) is None
    assert memory_note(Machine(8, PLENTY, DESKTOP)) is None
    assert memory_note(Machine(8, Memory(4 * GIB, None), DESKTOP)) is None


# --- The scan, end to end -----------------------------------------------------------------------


def test_scan_reads_the_given_files(tmp_path: Path) -> None:
    found = scan_machine(
        meminfo=meminfo(tmp_path / "meminfo", total_gib=8, available_gib=3),
        power_supply=power_supply(tmp_path / "power_supply", ON_BATTERY),
    )

    assert found.memory == Memory(8 * GIB, 3 * GIB)
    assert found.power == Power(battery=True, on_battery=True)
    assert found.cpus >= 1


def test_scan_never_raises_on_a_machine_with_nothing_to_read(tmp_path: Path) -> None:
    found = scan_machine(meminfo=tmp_path / "none", power_supply=tmp_path / "none")

    assert (found.memory, found.power) == (Memory(), Power())


def test_the_engine_uses_the_scans_thread_count_and_rereads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(machine, "cpu_count", lambda: 16)
    pin_machine(monkeypatch, tmp_path / "plugged", supplies=PLUGGED_IN)
    assert default_threads() == 8

    pin_machine(monkeypatch, tmp_path / "unplugged", supplies=ON_BATTERY)
    assert default_threads() == 4

    pin_machine(monkeypatch, tmp_path / "small", total_gib=3.7, available_gib=1.5)
    assert default_threads() == 4
