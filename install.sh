#!/bin/sh
# Tomecast installer: https://github.com/Kingskid2026/tomecast
#
#   curl -fsSL https://raw.githubusercontent.com/Kingskid2026/tomecast/main/install.sh | sh
#
# What this does (no sudo, everything stays in your home folder):
#   1. Installs uv, the Python tool manager, into ~/.local/bin if you don't have it.
#      uv adds ~/.local/bin to your PATH in your shell's startup file.
#   2. Uses uv to install Tomecast from PyPI. uv downloads a suitable Python if needed.
#   3. Runs `tomecast --version` to check that it worked, and makes sure the `tomecast`
#      command will be on your PATH in new terminals.
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

if command -v uv >/dev/null 2>&1; then
    UV="uv"
    say "using the uv already on your system ($(uv --version))"
else
    say "installing uv $UV_VERSION (the tool Tomecast uses to manage Python) into ~/.local/bin"
    download "https://astral.sh/uv/$UV_VERSION/install.sh" | sh
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || fail "uv did not install correctly. See https://docs.astral.sh/uv/ for help."
fi

if "$UV" tool list 2>/dev/null | grep -q '^tomecast '; then
    say "Tomecast is already installed; upgrading it"
    "$UV" tool upgrade tomecast
else
    say "installing Tomecast"
    "$UV" tool install "$PACKAGE"
fi

BIN_DIR="$("$UV" tool dir --bin)"
"$BIN_DIR/tomecast" --version >/dev/null || fail "Tomecast installed, but didn't start. Please open an issue."

say "done! $("$BIN_DIR/tomecast" --version) is installed."
case ":$PATH:" in
    *":$BIN_DIR:"*) say "run 'tomecast --help' to get started." ;;
    *)
        # Adds uv's tool folder to PATH in your shell's startup file (bash, zsh, fish…).
        "$UV" tool update-shell >/dev/null 2>&1 || true
        say "open a new terminal, then run 'tomecast --help' to get started."
        ;;
esac
