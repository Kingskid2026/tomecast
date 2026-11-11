#!/bin/sh
# Tomecast installer: https://github.com/Kingskid2026/tomecast
#
#   curl -fsSL https://raw.githubusercontent.com/Kingskid2026/tomecast/main/install.sh | sh
#
# What this does (no sudo, everything stays in your home folder):
#   1. Installs uv, the Python tool manager, into ~/.local/bin if you don't have it (or
#      where you've asked for it: UV_INSTALL_DIR, XDG_BIN_HOME or XDG_DATA_HOME/../bin).
#      uv adds that folder to your PATH in your shell's startup file.
#   2. Uses uv to install Tomecast from PyPI. uv downloads a suitable Python if needed.
#   3. Runs `tomecast --version` to check that it worked, and makes sure the `tomecast`
#      command will be on your PATH in new terminals.
#   4. Adds Tomecast to your app launcher (GNOME, KDE, Hyprland launchers and others) and to
#      the "Open with" menu for EPUB files, by writing two small files under ~/.local/share.
#
# Tomecast downloads its voice engines and models later, on first launch, after asking.
set -eu

UV_VERSION="0.13.7"
# CI points this at a local checkout to test unreleased code.
PACKAGE="${TOMECAST_PACKAGE:-tomecast}"

say() { printf 'tomecast-install: %s\n' "$1"; }
fail() {
    printf 'tomecast-install: error: %s\n' "$1" >&2
    exit 1
}

[ "$(uname -s)" = "Linux" ] || fail "Tomecast only supports Linux."

download() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        fail "this installer needs curl or wget. Install one with your package manager and try again."
    fi
}

# Where uv's installer put uv: in the first of these folders that is set (its --help lists
# them; UV_UNMANAGED_INSTALL and CARGO_DIST_FORCE_INSTALL_DIR are other names for the first).
installed_uv() {
    for dir in "${UV_INSTALL_DIR:-}" "${CARGO_DIST_FORCE_INSTALL_DIR:-}" \
        "${UV_UNMANAGED_INSTALL:-}" "${XDG_BIN_HOME:-}" "${XDG_DATA_HOME:+$XDG_DATA_HOME/../bin}" \
        "$HOME/.local/bin"; do
        if [ -n "$dir" ] && [ -x "$dir/uv" ]; then
            printf '%s\n' "$dir/uv"
            return 0
        fi
    done
    return 1
}

if command -v uv >/dev/null 2>&1; then
    UV="uv"
    say "using the uv already on your system ($(uv --version))"
else
    say "installing uv $UV_VERSION (the tool Tomecast uses to manage Python)"
    download "https://astral.sh/uv/$UV_VERSION/install.sh" | sh
    UV="$(installed_uv)" || fail "uv did not install correctly. See https://docs.astral.sh/uv/ for help."
fi

# uv remembers PyPI's package list for a few minutes; these flags ask again for Tomecast only,
# so a release published moments ago is picked up. Older uv versions get the plain command.
if "$UV" tool list 2>/dev/null | grep -q '^tomecast '; then
    say "Tomecast is already installed; upgrading it"
    "$UV" tool upgrade --reinstall-package tomecast tomecast 2>/dev/null || "$UV" tool upgrade tomecast
else
    say "installing Tomecast"
    "$UV" tool install --refresh-package tomecast "$PACKAGE" 2>/dev/null || "$UV" tool install "$PACKAGE"
fi

BIN_DIR="$("$UV" tool dir --bin)"
"$BIN_DIR/tomecast" --version >/dev/null || fail "Tomecast installed, but didn't start. Please open an issue."

say "done! $("$BIN_DIR/tomecast" --version) is installed."

# Add Tomecast to the app launcher and the EPUB "Open with" menu. Optional: never fails the install.
if "$BIN_DIR/tomecast" desktop-entry install >/dev/null 2>&1; then
    say "added Tomecast to your app launcher: open it and search for 'Tomecast'."
else
    say "couldn't add Tomecast to your app launcher. Try 'tomecast desktop-entry install' later."
fi
case ":$PATH:" in
    *":$BIN_DIR:"*) say "run 'tomecast --help' to get started." ;;
    *)
        # Adds uv's tool folder to PATH in your shell's startup file (bash, zsh, fish…).
        "$UV" tool update-shell >/dev/null 2>&1 || true
        say "open a new terminal, then run 'tomecast --help' to get started."
        ;;
esac
