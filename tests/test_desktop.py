import shutil
import subprocess
from pathlib import Path

import pytest

from tomecast import desktop


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
    assert any("app launcher" in message for message in messages)


def test_path_with_spaces_is_quoted() -> None:
    text = desktop.desktop_entry_text("/home/a b/bin/tomecast")
    assert 'Exec="/home/a b/bin/tomecast" gui %f' in text.splitlines()
    assert "TryExec=/home/a b/bin/tomecast" in text.splitlines()


def test_special_characters_in_exec_are_escaped() -> None:
    assert desktop.quote_exec('/x/a"b$c') == '"/x/a\\"b\\$c"'
    assert desktop.quote_exec("/x/100%") == "/x/100%%"


def test_falls_back_to_argv0(
    data_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = fake_tomecast(tmp_path / "elsewhere")
    monkeypatch.setattr("sys.argv", [str(program)])
    assert desktop.find_executable() == str(program.resolve())


def test_uninstall_removes_both_files(data_home: Path) -> None:
    desktop.install()
    messages = desktop.uninstall()
    assert not (data_home / "applications" / "tomecast.desktop").exists()
    assert not (data_home / "icons/hicolor/scalable/apps/tomecast.svg").exists()
    assert len(messages) == 2


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
    assert "PLACEHOLDER" in text
    assert "<script" not in text
    assert "http" not in text.replace("http://www.w3.org/2000/svg", "")
