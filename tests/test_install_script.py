"""install.sh, run against stand-ins for curl, uv's installer, uv and Tomecast (no network).

CI also runs the real script in clean distro containers; these cover what those don't: the
folders a user can choose for uv."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"

# What uv's own installer (0.13.7, the one install.sh pins) does with its install folder: the
# first of these that is set, else ~/.local/bin (see its --help).
FAKE_UV_INSTALLER = """\
if [ -n "${UV_INSTALL_DIR:-}" ]; then dir="$UV_INSTALL_DIR"
elif [ -n "${XDG_BIN_HOME:-}" ]; then dir="$XDG_BIN_HOME"
elif [ -n "${XDG_DATA_HOME:-}" ]; then dir="$XDG_DATA_HOME/../bin"
else dir="$HOME/.local/bin"
fi
mkdir -p "$dir"
cp "$FAKE_UV" "$dir/uv"
chmod +x "$dir/uv"
echo "installed uv into $dir"
"""

FAKE_UV = """\
#!/bin/sh
echo "$0 $*" >> "$FAKE_LOG"
case "$*" in
    --version) echo "uv 0.13.7" ;;
    "tool list") ;;
    "tool install"*)
        mkdir -p "$FAKE_TOOL_BIN"
        cp "$FAKE_TOMECAST" "$FAKE_TOOL_BIN/tomecast"
        chmod +x "$FAKE_TOOL_BIN/tomecast"
        ;;
    "tool dir --bin") echo "$FAKE_TOOL_BIN" ;;
    "tool update-shell") ;;
    *) echo "fake uv: unexpected arguments: $*" >&2; exit 2 ;;
esac
"""

FAKE_TOMECAST = """\
#!/bin/sh
case "$1" in
    --version) echo "tomecast 9.9.9" ;;
    desktop-entry) ;;
esac
"""


def _script(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def _run_installer(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run install.sh with only the tools it needs on PATH, so the computer's own uv (if it
    has one) isn't found and the script installs the fake one."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ["sh", "uname", "grep", "cat", "mkdir", "chmod", "cp"]:
        real = shutil.which(tool)
        assert real is not None, tool
        (tools / tool).symlink_to(real)
    _script(tools / "curl", '#!/bin/sh\ncat "$FAKE_UV_INSTALLER"\n')
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "uv-installer.sh").write_text(FAKE_UV_INSTALLER)
    home = tmp_path / "home"
    home.mkdir()
    return subprocess.run(
        ["sh", str(INSTALL_SH)],
        env={
            "HOME": str(home),
            "PATH": str(tools),
            "FAKE_UV_INSTALLER": str(fakes / "uv-installer.sh"),
            "FAKE_UV": str(_script(fakes / "uv", FAKE_UV)),
            "FAKE_TOMECAST": str(_script(fakes / "tomecast", FAKE_TOMECAST)),
            "FAKE_TOOL_BIN": str(tmp_path / "tool-bin"),
            "FAKE_LOG": str(tmp_path / "uv.log"),
            **env,
        },
        executable=shutil.which("sh"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    ("env", "uv_folder"),
    [
        ({}, "home/.local/bin"),
        ({"XDG_BIN_HOME": "{tmp}/my-bin"}, "my-bin"),
        ({"XDG_DATA_HOME": "{tmp}/data/share"}, "data/share/../bin"),
        ({"UV_INSTALL_DIR": "{tmp}/uv-here"}, "uv-here"),
        ({"UV_INSTALL_DIR": "{tmp}/uv-here", "XDG_BIN_HOME": "{tmp}/my-bin"}, "uv-here"),
    ],
)
def test_installs_uv_and_then_tomecast_with_it_wherever_uv_went(
    tmp_path: Path, env: dict[str, str], uv_folder: str
) -> None:
    env = {name: value.format(tmp=tmp_path) for name, value in env.items()}
    if "XDG_DATA_HOME" in env:
        os.makedirs(env["XDG_DATA_HOME"])

    run = _run_installer(tmp_path, env)

    assert run.returncode == 0, run.stdout + run.stderr
    assert "done! tomecast 9.9.9 is installed." in run.stdout
    uv = tmp_path / uv_folder / "uv"
    assert uv.is_file()
    log = (tmp_path / "uv.log").read_text().splitlines()
    assert f"{uv} tool install --refresh-package tomecast tomecast" in log


def test_says_so_when_uv_didnt_install(tmp_path: Path) -> None:
    run = _run_installer(tmp_path, {"FAKE_UV": str(tmp_path / "missing")})

    assert run.returncode == 1
    assert "uv did not install correctly" in run.stderr
    assert not (tmp_path / "uv.log").exists()
