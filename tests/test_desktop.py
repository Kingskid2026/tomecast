import shutil
import subprocess
from pathlib import Path

import pytest

from tomecast import desktop

# The freedesktop launcher entry is Linux's; Windows gets a Start Menu shortcut (#39).
pytestmark = pytest.mark.posix_only


@pytest.fixture
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))  # no cache tools by default
    return tmp_path / "share"


def fake_tomecast(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / "tomecast"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    return program


def test_install_writes_entry_and_icon(
    data_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = fake_tomecast(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(program.parent))
    messages = desktop.install()

    entry = (data_home / "applications" / "tomecast.desktop").read_text()
    assert f"Exec={program} gui %f\n" in entry
    assert f"TryExec={program}\n" in entry
    for line in (
        "Type=Application",
        "Name=Tomecast",
        "GenericName=Audiobook maker",
        "Comment=Turn ebooks into audiobooks on this computer",
        "Icon=tomecast",
        "Terminal=false",
        "Categories=AudioVideo;Audio;Office;",
        "Keywords=audiobook;ebook;epub;tts;text to speech;voice;",
        "MimeType=application/epub+zip;",
        "StartupWMClass=tomecast",
        "StartupNotify=true",
    ):
        assert line in entry.splitlines()
    icon = data_home / "icons" / "hicolor" / "scalable" / "apps" / "tomecast.svg"
    assert icon.read_text().startswith("<?xml")
    assert "<svg" in icon.read_text()
    for size in desktop.ICON_SIZES:
        png = data_home / "icons" / "hicolor" / f"{size}x{size}" / "apps" / "tomecast.png"
        assert png.read_bytes() == (desktop._PNG_SOURCES[size]).read_bytes()
    assert any("app launcher" in message for message in messages)


def test_path_with_spaces_is_quoted() -> None:
    text = desktop.desktop_entry_text("/home/a b/bin/tomecast")
    assert 'Exec="/home/a b/bin/tomecast" gui %f' in text.splitlines()
    assert "TryExec=/home/a b/bin/tomecast" in text.splitlines()


def test_special_characters_in_exec_are_escaped() -> None:
    # Quoting layer (\" \$), then the string layer doubles each backslash.
    assert desktop.quote_exec('/x/a"b$c') == r'"/x/a\\"b\\$c"'
    assert desktop.quote_exec("/x/100%") == "/x/100%%"


def test_falls_back_to_argv0(
    data_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = fake_tomecast(tmp_path / "elsewhere")
    monkeypatch.setattr("sys.argv", [str(program)])
    assert desktop.find_executable() == str(program.resolve())


def test_uninstall_removes_entry_icon_and_pngs(data_home: Path) -> None:
    desktop.install()
    messages = desktop.uninstall()
    assert not (data_home / "applications" / "tomecast.desktop").exists()
    assert not (data_home / "icons/hicolor/scalable/apps/tomecast.svg").exists()
    for size in desktop.ICON_SIZES:
        assert not (data_home / "icons" / "hicolor" / f"{size}x{size}/apps/tomecast.png").exists()
    assert len(messages) == 3  # the entry, the SVG, and one line for the PNGs


def test_uninstall_with_nothing_installed(data_home: Path) -> None:
    assert "nothing to remove" in desktop.uninstall()[0]


def test_uninstall_leaves_someone_elses_files(data_home: Path) -> None:
    entry = data_home / "applications" / "tomecast.desktop"
    icon = data_home / "icons/hicolor/scalable/apps/tomecast.svg"
    entry.parent.mkdir(parents=True)
    icon.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nName=Something else\n")
    icon.write_text("<svg><title>Other</title></svg>")
    messages = desktop.uninstall()
    assert entry.exists()
    assert icon.exists()
    assert all("alone" in message for message in messages)


def test_uninstall_leaves_someone_elses_png_alone(data_home: Path) -> None:
    desktop.install()
    other = data_home / "icons/hicolor/256x256/apps/tomecast.png"
    other.write_bytes(b"not ours")
    messages = desktop.uninstall()
    assert other.read_bytes() == b"not ours"
    assert any("256x256" in message and "alone" in message for message in messages)
    assert not (data_home / "icons/hicolor/16x16/apps/tomecast.png").exists()


def test_missing_cache_tools_are_fine(data_home: Path) -> None:
    desktop.install()
    desktop.uninstall()


def test_failing_cache_tools_are_ignored(
    data_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("subprocess.run", explode)
    desktop.install()


def test_cache_tools_run_when_present(data_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("subprocess.run", lambda command, **kwargs: calls.append(list(command)))
    desktop.install()
    assert calls[0] == ["update-desktop-database", str(data_home / "applications")]
    assert calls[1][:3] == ["gtk-update-icon-cache", "-f", "-t"]
    assert calls[1][3] == str(data_home / "icons" / "hicolor")


def test_generated_entry_passes_desktop_file_validate(tmp_path: Path) -> None:
    validator = shutil.which("desktop-file-validate")
    if validator is None:
        pytest.skip("desktop-file-validate isn't installed")
    entry = tmp_path / "tomecast.desktop"
    entry.write_text(desktop.desktop_entry_text("/usr/bin/tomecast"))
    result = subprocess.run([validator, str(entry)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_icon_is_a_clean_svg() -> None:
    text = (Path(desktop.__file__).parent / "gui/icons/tomecast.svg").read_text()
    assert "PLACEHOLDER" not in text
    assert "<title>Tomecast</title>" in text  # how uninstall recognises the installed copy
    assert text.count("<circle") == 25
    assert "<script" not in text
    assert "http" not in text.replace("http://www.w3.org/2000/svg", "")


@pytest.mark.parametrize("size", desktop.ICON_SIZES)
def test_png_sizes_are_real_pngs_of_the_right_size(size: int) -> None:
    data = (desktop._PNG_SOURCES[size]).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # The IHDR chunk comes first: width and height are big-endian 32-bit ints at byte 16.
    assert int.from_bytes(data[16:20], "big") == size
    assert int.from_bytes(data[20:24], "big") == size


@pytest.mark.parametrize("size", [16, 24])
def test_small_icon_drawings_are_the_same_grid_snapped_to_pixels(size: int) -> None:
    # Same design as tomecast.svg: 25 dots, 12 voiced, the centre one speaking, 12 waiting.
    icons = desktop._ICON_SOURCE.parent
    text = (icons / f"tomecast-{size}.svg").read_text(encoding="utf-8")
    assert "<title>Tomecast</title>" in text
    assert text.count("<circle") == 25
    assert text.count('fill="#F2F2F2"') == 12
    assert text.count('fill="#7C8CFF"') == 1
    assert text.count('fill="#2A2A2E"') == 12
    assert f'viewBox="0 0 {size} {size}"' in text


NASTY_PATHS = ['/x/a"b', "/x/a`b", "/x/a$b", "/x/a\\b", '/x/$HOME/"q"/`id`/\\n a b', "/x/100%/y"]


@pytest.mark.parametrize("path", [p for p in NASTY_PATHS if "%" not in p])
def test_exec_quoting_matches_the_two_layer_spec(path: str) -> None:
    quoted = desktop.quote_exec(path)
    expected = "".join("\\" + c if c in '"`$\\' else c for c in path)
    expected = expected.replace("\\", "\\\\").replace("%", "%%")
    assert quoted == f'"{expected}"'


@pytest.mark.parametrize("path", NASTY_PATHS)
def test_exec_round_trips_through_glib(path: str) -> None:
    glib = pytest.importorskip("gi.repository.GLib")
    line = next(
        entry_line
        for entry_line in desktop.desktop_entry_text(path).splitlines()
        if entry_line.startswith("Exec=")
    )
    # Undo the string layer (a doubled backslash is one), and %% is a literal %.
    value = line.removeprefix("Exec=").replace("\\\\", "\\").replace("%%", "%")
    assert glib.shell_parse_argv(value)[1] == [path, "gui", "%f"]


@pytest.mark.parametrize("path", NASTY_PATHS)
def test_nasty_paths_pass_desktop_file_validate(path: str, tmp_path: Path) -> None:
    validator = shutil.which("desktop-file-validate")
    if validator is None:
        pytest.skip("desktop-file-validate isn't installed")
    entry = tmp_path / "tomecast.desktop"
    entry.write_text(desktop.desktop_entry_text(path))
    result = subprocess.run([validator, str(entry)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_install_refuses_to_overwrite_someone_elses_entry(data_home: Path) -> None:
    entry = data_home / "applications" / "tomecast.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nName=Something else\n")
    with pytest.raises(desktop.ForeignEntryError, match="already exists"):
        desktop.install()
    assert entry.read_text() == "[Desktop Entry]\nName=Something else\n"
    assert not (data_home / "icons").exists()


def test_install_updates_its_own_entry(data_home: Path) -> None:
    desktop.install()
    desktop.install()
    assert desktop.is_ours(data_home / "applications" / "tomecast.desktop")
