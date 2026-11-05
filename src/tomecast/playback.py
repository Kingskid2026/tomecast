"""Play a WAV file through whatever audio player the system has."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PLAYERS = ("pw-play", "paplay", "aplay")  # the first one found plays previews


def find_player() -> str | None:
    """The system audio player: pw-play, then paplay, then aplay."""
    return next((path for name in PLAYERS if (path := shutil.which(name))), None)


def play(wav: Path) -> None:
    """Play ``wav`` and wait for it to finish.

    Raises:
        RuntimeError: if there is no audio player, or it fails.
    """
    player = find_player()
    if player is None:
        raise RuntimeError(f"no audio player found ({', '.join(PLAYERS)})")
    try:
        result = subprocess.run(
            [player, str(wav)], stdin=subprocess.DEVNULL, capture_output=True, check=False
        )
    except OSError as error:
        raise RuntimeError(f"couldn't run {player}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"{Path(player).name} failed" + (f": {detail}" if detail else ""))
