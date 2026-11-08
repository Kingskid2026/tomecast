"""Launcher integration: the freedesktop ``.desktop`` entry and icon for ``tomecast gui``.

Installing writes a ``.desktop`` entry and the icon (SVG, plus PNG sizes) under
``$XDG_DATA_HOME`` (default ``~/.local/share``) so Tomecast shows up in app launchers (Walker,
GNOME Activities, KRunner) and in the "Open with" menu for EPUB files. Nothing here needs root.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ENTRY_NAME = "tomecast.desktop"
ICON_NAME = "tomecast.svg"
# PNG copies for docks and older themes that don't read SVG. Keep in step with
# scripts/render_icons.py, which makes them from the SVG.
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
# Only files containing this line are treated as ours, so uninstall never deletes
# someone else's tomecast.desktop.
MARKER = "Name=Tomecast"
_ICON_SOURCE = Path(__file__).parent / "gui" / "icons" / ICON_NAME
_PNG_SOURCES = {size: _ICON_SOURCE.parent / "png" / f"tomecast-{size}.png" for size in ICON_SIZES}


class ForeignEntryError(RuntimeError):
    """``tomecast.desktop`` exists and belongs to something else."""


@dataclass(frozen=True)
class Locations:
    """Where the launcher files go."""

    entry: Path
    icon: Path

    @property
    def applications(self) -> Path:
        return self.entry.parent

    @property
    def hicolor(self) -> Path:
        return self.icon.parents[2]

    def png(self, size: int) -> Path:
        return self.hicolor / f"{size}x{size}" / "apps" / "tomecast.png"


def locations() -> Locations:
    """The entry and icon paths, following ``$XDG_DATA_HOME``."""
    configured = os.environ.get("XDG_DATA_HOME", "")
    base = Path(configured) if configured and Path(configured).is_absolute() else None
    data = base or Path.home() / ".local" / "share"
    return Locations(
        entry=data / "applications" / ENTRY_NAME,
        icon=data / "icons" / "hicolor" / "scalable" / "apps" / ICON_NAME,
    )


def quote_exec(path: str) -> str:
    """Quote a program path for an ``Exec=`` line, per the Desktop Entry specification."""
    if not any(c in path for c in " \t\n\"'\\><~|&;$*?#()`"):
        return path.replace("%", "%%")
    # Two layers, in this order. First the quoting layer: inside double quotes, a backslash
    # must precede ", `, $ and \. Then the string layer: every backslash in the file is
    # written doubled. (So one literal backslash in the path ends up as four.)
    quoted = "".join("\\" + c if c in '"`$\\' else c for c in path)
    escaped = quoted.replace("\\", "\\\\").replace("%", "%%")
    return f'"{escaped}"'


def desktop_entry_text(executable: str) -> str:
    """The text of ``tomecast.desktop`` for the given absolute path to ``tomecast``."""
    program = quote_exec(executable)
    # TryExec is a plain path, not an Exec-style command line, so it is not quoted.
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        MARKER,
        "GenericName=Audiobook maker",
        "Comment=Turn ebooks into audiobooks on this computer",
        f"Exec={program} gui %f",
        f"TryExec={executable.replace(chr(92), chr(92) * 2)}",
        "Icon=tomecast",
        "Terminal=false",
        "Categories=AudioVideo;Audio;Office;",
        "Keywords=audiobook;ebook;epub;tts;text to speech;voice;",
        "MimeType=application/epub+zip;",
        "StartupWMClass=tomecast",
        "StartupNotify=true",
    ]
    return "\n".join(lines) + "\n"


def find_executable() -> str:
    """The absolute path of the ``tomecast`` command to launch."""
    found = shutil.which("tomecast")
    return str(Path(found if found else sys.argv[0]).resolve())


def install() -> list[str]:
    """Write the entry and icon, refresh launcher caches, and describe what was done."""
    where = locations()
    if where.entry.exists() and not is_ours(where.entry):
        raise ForeignEntryError(
            f"{where.entry} already exists and wasn't created by Tomecast, so it was left "
            "alone. Move or remove it, then run this again."
        )
    executable = find_executable()
    where.entry.parent.mkdir(parents=True, exist_ok=True)
    where.icon.parent.mkdir(parents=True, exist_ok=True)
    where.entry.write_text(desktop_entry_text(executable), encoding="utf-8")
    shutil.copyfile(_ICON_SOURCE, where.icon)
    for size, source in _PNG_SOURCES.items():
        target = where.png(size)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    refresh_caches(where)
    return [
        f"Added Tomecast to your app launcher: {where.entry}",
        f"Installed its icon: {where.icon}, and PNG sizes {', '.join(map(str, ICON_SIZES))}",
        f"It opens {executable} gui, and EPUB files now offer Open with Tomecast.",
    ]


def is_ours(entry: Path) -> bool:
    """Whether ``entry`` is a Tomecast launcher entry (and not someone else's file)."""
    try:
        return MARKER in entry.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False


def uninstall() -> list[str]:
    """Remove the entry and icon if they are Tomecast's, and describe what was done."""
    where = locations()
    messages: list[str] = []
    if where.entry.exists():
        if is_ours(where.entry):
            where.entry.unlink()
            messages.append(f"Removed {where.entry}")
        else:
            messages.append(f"Left {where.entry} alone, because Tomecast didn't create it.")
    if where.icon.exists():
        if _icon_is_ours(where.icon):
            where.icon.unlink()
            messages.append(f"Removed {where.icon}")
        else:
            messages.append(f"Left {where.icon} alone, because Tomecast didn't create it.")
    # A PNG has no title to check, so a file counts as ours only if it is byte-for-byte the
    # copy we install.
    removed_pngs = 0
    for size, source in _PNG_SOURCES.items():
        target = where.png(size)
        if not target.exists():
            continue
        if _same_bytes(target, source):
            target.unlink()
            removed_pngs += 1
        else:
            messages.append(f"Left {target} alone, because Tomecast didn't create it.")
    if removed_pngs:
        messages.append(f"Removed Tomecast's {removed_pngs} PNG icons from {where.hicolor}")
    if not messages:
        return ["Tomecast wasn't in your app launcher, so there is nothing to remove."]
    refresh_caches(where)
    return messages


def _icon_is_ours(icon: Path) -> bool:
    try:
        return "<title>Tomecast</title>" in icon.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _same_bytes(path: Path, source: Path) -> bool:
    try:
        return path.read_bytes() == source.read_bytes()
    except OSError:
        return False


def refresh_caches(where: Locations) -> None:
    """Ask the desktop to notice the change. Missing or failing tools are ignored."""
    commands = [
        ["update-desktop-database", str(where.applications)],
        ["gtk-update-icon-cache", "-f", "-t", str(where.hicolor)],
    ]
    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(command, check=False, capture_output=True, timeout=30)
