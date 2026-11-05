import subprocess
from pathlib import Path

import pytest

from tomecast import playback


def test_the_first_player_found_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    found = {"paplay": "/usr/bin/paplay", "aplay": "/usr/bin/aplay"}
    monkeypatch.setattr("shutil.which", found.get)
    assert playback.find_player() == "/usr/bin/paplay"


def test_no_player_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert playback.find_player() is None
    with pytest.raises(RuntimeError, match="no audio player"):
        playback.play(Path("x.wav"))


def test_play_runs_the_player_with_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("subprocess.run", run)
    playback.play(Path("/tmp/word.wav"))
    assert calls == [["/bin/pw-play", "/tmp/word.wav"]]


def test_a_failing_player_is_reported_with_its_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, b"", b"connection refused\n")

    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("subprocess.run", run)
    with pytest.raises(RuntimeError, match="pw-play failed: connection refused"):
        playback.play(Path("x.wav"))


def test_a_player_that_cannot_start_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise PermissionError("denied")

    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("subprocess.run", run)
    with pytest.raises(RuntimeError, match="couldn't run"):
        playback.play(Path("x.wav"))
