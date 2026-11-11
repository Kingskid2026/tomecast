import ctypes
import glob
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tomecast.hardware import GpuDevice, list_devices, pick_device, short_name, vulkan_possible

GB = 1_000_000_000


def _device(index: int, kind: str, free: int) -> GpuDevice:
    return GpuDevice(index, f"Vulkan{index}", "Some card", kind, free, free * 2)


# --- vulkan_possible --------------------------------------------------------------------------


def test_vulkan_is_possible_with_a_loader_and_a_device_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[str] = []
    monkeypatch.setattr(ctypes, "CDLL", lambda name: loaded.append(name))
    monkeypatch.setattr(
        glob,
        "glob",
        lambda pattern: ["/dev/dri/renderD128"] if "renderD" in pattern else [],
    )
    assert vulkan_possible() is True
    assert loaded == ["libvulkan.so.1"]


def test_an_nvidia_node_alone_is_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctypes, "CDLL", lambda name: None)
    monkeypatch.setattr(
        glob, "glob", lambda pattern: ["/dev/nvidia0"] if "nvidia" in pattern else []
    )
    assert vulkan_possible() is True


def test_vulkan_is_not_possible_without_the_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> None:
        raise OSError("libvulkan.so.1: cannot open shared object file")

    monkeypatch.setattr(ctypes, "CDLL", missing)
    monkeypatch.setattr(glob, "glob", lambda pattern: ["/dev/dri/renderD128"])
    assert vulkan_possible() is False


def test_vulkan_is_not_possible_without_a_graphics_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctypes, "CDLL", lambda name: None)
    monkeypatch.setattr(glob, "glob", lambda pattern: [])
    assert vulkan_possible() is False


# --- list_devices -----------------------------------------------------------------------------

_LISTING = (
    '{"schema_version":1,"devices":['
    '{"backend":"vulkan","index":0,"name":"Vulkan0","type":"gpu",'
    '"description":"NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU",'
    '"memory":{"free_bytes":7333281792,"total_bytes":8546942976}},'
    '{"backend":"vulkan","index":1,"name":"Vulkan1","type":"igpu",'
    '"description":"AMD Radeon 860M Graphics (RADV KRACKAN1)",'
    '"memory":{"free_bytes":9000000000,"total_bytes":20000000000}},'
    '{"backend":"cpu","index":0,"name":"CPU","type":"cpu","description":"AMD Ryzen",'
    '"memory":{"free_bytes":1,"total_bytes":2}}]}'
)


def _executable(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "audiocpp_server"
    script.write_text(f"#!{sys.executable}\n{textwrap.dedent(body)}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.mark.posix_only
def test_lists_only_the_vulkan_devices(tmp_path: Path) -> None:
    script = _executable(
        tmp_path,
        f"""
        import sys
        assert sys.argv[1:] == ["--backend", "vulkan", "--list-devices", "--json"], sys.argv
        print("ggml_vulkan: Found 2 Vulkan devices:", file=sys.stderr)
        print("{{not json}}")
        print({_LISTING!r})
        print("trailing log line, not json")
        """,
    )

    assert list_devices(script) == [
        GpuDevice(
            0,
            "Vulkan0",
            "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU",
            "gpu",
            7_333_281_792,
            8_546_942_976,
        ),
        GpuDevice(
            1,
            "Vulkan1",
            "AMD Radeon 860M Graphics (RADV KRACKAN1)",
            "igpu",
            9_000_000_000,
            20_000_000_000,
        ),
    ]


@pytest.mark.posix_only
@pytest.mark.parametrize(
    "body",
    [
        "import sys; sys.exit(3)",
        "print('no json at all')",
        "print('[1, 2, 3]')",
        "print('{\"devices\": 5}')",
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
        "",
    ],
)
def test_a_crash_or_odd_output_is_not_knowing_not_no_card(tmp_path: Path, body: str) -> None:
    assert list_devices(_executable(tmp_path, body)) is None  # "couldn't tell", not "none"


@pytest.mark.posix_only
def test_an_answer_with_nothing_usable_is_no_card(tmp_path: Path) -> None:
    junk = '{"devices": [5, {"backend": "vulkan"}, {"backend": "vulkan", "index": 1}]}'
    assert list_devices(_executable(tmp_path, f"print({junk!r})")) == []
    assert list_devices(_executable(tmp_path, "print('{\"devices\": []}')")) == []


def test_a_missing_executable_cant_tell(tmp_path: Path) -> None:
    assert list_devices(tmp_path / "nothing-here") is None


@pytest.mark.posix_only
def test_a_hung_executable_times_out_and_cant_tell(tmp_path: Path) -> None:
    script = _executable(tmp_path, "import time; time.sleep(30)")
    started = time.monotonic()
    assert list_devices(script, timeout=0.5) is None  # a stalled card isn't "no card"
    assert time.monotonic() - started < 5


@pytest.mark.posix_only
def test_listing_stops_early_when_the_user_stops(tmp_path: Path) -> None:
    script = _executable(tmp_path, "import time; time.sleep(30)")
    started = time.monotonic()
    stop_at = started + 0.3

    assert list_devices(script, cancelled=lambda: time.monotonic() > stop_at) is None
    assert time.monotonic() - started < 3  # not the 20 s time limit


@pytest.mark.posix_only
def test_missing_memory_counts_as_zero(tmp_path: Path) -> None:
    script = _executable(
        tmp_path,
        """
        print('{"devices":[{"backend":"vulkan","index":2,"name":"Vulkan2","type":"gpu"}]}')
        """,
    )
    assert list_devices(script) == [GpuDevice(2, "Vulkan2", "Vulkan2", "gpu", 0, 0)]


# --- pick_device ------------------------------------------------------------------------------


def test_picks_the_discrete_card_with_the_most_free_memory() -> None:
    picked = pick_device(
        [_device(0, "gpu", 4 * GB), _device(1, "igpu", 20 * GB), _device(2, "gpu", 7 * GB)]
    )
    assert picked is not None
    assert picked.index == 2


def test_picks_a_built_in_card_when_there_is_no_discrete_one() -> None:
    picked = pick_device([_device(0, "igpu", 3 * GB), _device(1, "igpu", 9 * GB)])
    assert picked is not None
    assert picked.index == 1


def test_a_built_in_card_needs_two_gigabytes_free() -> None:
    assert pick_device([_device(0, "igpu", 1_999_999_999)]) is None
    assert pick_device([_device(0, "igpu", 2 * GB)]) is not None


def test_a_discrete_card_is_used_even_with_little_memory() -> None:
    assert pick_device([_device(0, "gpu", 100_000_000)]) is not None


def test_picks_nothing_without_a_usable_card() -> None:
    assert pick_device([]) is None
    assert pick_device([_device(0, "cpu", 50 * GB)]) is None


# --- short_name -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "short"),
    [
        ("NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU", "NVIDIA RTX PRO 1000"),
        ("AMD Radeon 860M Graphics (RADV KRACKAN1)", "AMD RADEON 860M"),
        ("NVIDIA GeForce RTX 4060 Laptop GPU", "NVIDIA GEFORCE RTX 4060"),
        ("Intel(R) Arc(tm) A770 Graphics (DG2)", "INTEL ARC A770"),
        ("Graphics", "GRAPHICS"),
    ],
)
def test_short_names(description: str, short: str) -> None:
    assert short_name(description) == short
