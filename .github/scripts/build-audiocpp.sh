#!/usr/bin/env bash
# Build audio.cpp's voice-engine server on an old base system, so it runs on nearly every
# Linux distro from 2019 on. Upstream's own Linux binaries are built on the newest Ubuntu
# and need glibc 2.43 (https://github.com/Kingskid2026/tomecast/issues/18).
#
# Runs inside the manylinux_2_28 image (AlmaLinux 8: glibc 2.28, gcc-toolset-14), which
# .github/workflows/engine-build.yml pins by digest:
#
#   build-audiocpp.sh VARIANT SOURCE OUTPUT
#
#   VARIANT  cpu or vulkan
#   SOURCE   a clean checkout of https://github.com/0xShug0/audio.cpp at $AUDIOCPP_COMMIT,
#            which Tomecast's patches are applied to in place
#   OUTPUT   an empty folder for the bundle: audiocpp_server, LICENSE, model_specs/,
#            licenses/, patches/ and BUILD-INFO.txt (the official tarball's layout, minus the
#            tools Tomecast doesn't use)
#
# The audio.cpp source is built with upstream's release options, plus Tomecast's own patches
# from .github/patches/audiocpp/ (next to this script's folder), applied in name order. Each
# patch is opt-in: unless its environment variables are set, the server behaves exactly like
# upstream's. A patch that doesn't apply cleanly, or isn't compiled in, stops the build.
# The other differences from upstream's Linux release build are about where the result runs:
#   - it's built against glibc 2.28 instead of 2.43;
#   - libstdc++, libgcc and libgomp (OpenMP) are linked in statically, so the server only
#     needs glibc's own libraries, plus the system's libvulkan.so.1 for the Vulkan variant;
#   - the CPU kernels target AVX2-era CPUs (x86-64-v3) instead of whichever CPU the build
#     machine happened to have (-march=native).
#
# Optional environment: CCACHE_DIR (use ccache), VULKAN_TOOLS (where to keep what
# build-vulkan-tools.sh builds, so CI can cache it), BUILD_URL (for BUILD-INFO.txt).
set -euo pipefail

variant=${1:?usage: build-audiocpp.sh cpu|vulkan SOURCE OUTPUT}
src=$(realpath "${2:?missing SOURCE}")
out=${3:?missing OUTPUT}
: "${AUDIOCPP_VERSION:?set AUDIOCPP_VERSION, e.g. 0.9.4}"
: "${AUDIOCPP_COMMIT:?set AUDIOCPP_COMMIT to the full commit SHA of that release}"
case "$variant" in cpu | vulkan) ;; *) echo "unknown variant: $variant" >&2 && exit 2 ;; esac
jobs=$(nproc)
work=$(mktemp -d)
patch_dir=$(realpath -m "$(dirname "$(realpath "$0")")/../patches/audiocpp")
# The image's Pythons live in /opt/python; some build steps need a python3 on PATH.
command -v python3 >/dev/null || export PATH="/opt/python/cp312-cp312/bin:$PATH"

log() { printf '\n=== %s\n' "$*"; }

# --- Check the source ---------------------------------------------------------------------
# The checkout belongs to the runner's user; let git read it, so --version shows the commit,
# abbreviated to the same 8 characters as upstream's own builds.
git config --global --add safe.directory "$src"
git config --global core.abbrev 8
actual=$(git -C "$src" rev-parse HEAD)
if [ "$actual" != "$AUDIOCPP_COMMIT" ]; then
    echo "expected audio.cpp at $AUDIOCPP_COMMIT, found $actual" >&2
    exit 1
fi
if [ -n "$(git -C "$src" status --porcelain)" ]; then
    echo "the audio.cpp checkout has local changes; refusing to build" >&2
    exit 1
fi

# --- Apply Tomecast's patches --------------------------------------------------------------
# Each patch says in its header what it does, and lists strings its code puts in the server
# ("Binary-contains:"), which are checked once it's built. git apply takes no fuzz: a patch
# that doesn't match this exact source fails the build, rather than shipping without it.
if [ ! -d "$patch_dir" ]; then
    echo "no patch folder at $patch_dir; run this script from a Tomecast checkout" >&2
    exit 1
fi
mapfile -t patches < <(find "$patch_dir" -maxdepth 1 -type f -name '*.patch' | LC_ALL=C sort)
if [ "${#patches[@]}" -eq 0 ]; then
    # The release notes and BUILD-INFO.txt say the build carries patches: an empty folder is a
    # mistake, not "no patches". Delete the folder to build upstream unchanged.
    echo "no .patch files in $patch_dir; delete the folder to build without patches" >&2
    exit 1
fi
log "Applying ${#patches[@]} Tomecast patch(es) from $patch_dir"
for p in "${patches[@]}"; do
    if ! grep -qE '^Binary-contains: +[^ ]' "$p"; then
        echo "$(basename "$p") has no Binary-contains: line, so the build can't check it" >&2
        exit 1
    fi
    if ! git -C "$src" apply --verbose "$p"; then
        echo "$(basename "$p") doesn't apply to audio.cpp $AUDIOCPP_COMMIT; update or drop it" >&2
        exit 1
    fi
done
git -C "$src" diff --stat

# --- Build options ------------------------------------------------------------------------
# libgomp: the toolset's libgomp.so points at AlmaLinux's own GCC 8 runtime, which many
# minimal systems don't have. A folder holding only GCC 14's static libgomp.a, searched
# first, makes -fopenmp link that instead (the linker prefers .so over .a per folder).
static_libs="$work/static-libs"
mkdir -p "$static_libs"
cp "$(gcc -print-file-name=libgomp.a)" "$static_libs/"

cmake_args=(
    -DCMAKE_BUILD_TYPE=Release
    -DAUDIOCPP_VERSION="$AUDIOCPP_VERSION"
    # Upstream's release options (.github/workflows/release.yml at v0.9.4).
    -DAUDIOCPP_DEPLOYMENT_BUILD=ON
    -DAUDIOCPP_BUILD_NATIVE_MODEL_MANAGER=ON
    -DENGINE_ENABLE_LLAMAFILE=ON
    -DENGINE_ENABLE_CUDA_GRAPHS=ON
    -DENGINE_ENABLE_CUDA=OFF
    # Portability: AVX2-era CPU kernels instead of -march=native on the build machine.
    -DENGINE_ENABLE_NATIVE_CPU=OFF
    -DGGML_SSE42=ON -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON -DGGML_BMI2=ON
    # Portability: the C++ and OpenMP runtimes go inside the binary. The static libgomp
    # needs libdl and libpthread, which glibc 2.28 still has as separate libraries. They're
    # in the linker flags so CMake's own OpenMP check links the same way.
    "-DCMAKE_EXE_LINKER_FLAGS=-static-libstdc++ -static-libgcc -L$static_libs -Wl,--no-as-needed -ldl -lpthread"
    # No library search path baked in: the system's own libraries are the ones to use.
    -DCMAKE_SKIP_RPATH=ON
)
server=("$out/audiocpp_server")
if [ "$variant" = vulkan ]; then
    # Vulkan headers, a loader to link against, and glslc, built from pinned sources.
    tools=${VULKAN_TOOLS:-$work/vulkan-tools}
    tools_script="$(dirname "$(realpath "$0")")/build-vulkan-tools.sh"
    if [ "$(cat "$tools/.built-by" 2>/dev/null)" != "$(sha256sum "$tools_script" | cut -d' ' -f1)" ]; then
        log "Building the Vulkan headers, loader and glslc into $tools"
        "$tools_script" "$tools"
    else
        log "Using the Vulkan tools already built in $tools"
    fi
    export VULKAN_SDK=$tools
    export PATH="$tools/bin:$PATH"
    cmake_args+=(-DENGINE_ENABLE_VULKAN=ON)
    # The bundle has no library search path; here, point it at the build-time loader.
    server=(env "LD_LIBRARY_PATH=$tools/lib" "$out/audiocpp_server")
else
    cmake_args+=(-DENGINE_ENABLE_VULKAN=OFF)
fi
if [ -n "${CCACHE_DIR:-}" ]; then
    if ! command -v ccache >/dev/null; then
        dnf install -y -q ccache
    fi
    cmake_args+=(-DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache)
    ccache --zero-stats >/dev/null
fi

# --- Build --------------------------------------------------------------------------------
build="$work/build"
log "Configuring audio.cpp $AUDIOCPP_VERSION ($variant)"
if ! cmake -S "$src" -B "$build" "${cmake_args[@]}"; then
    tail -n 150 "$build/CMakeFiles/CMakeConfigureLog.yaml" >&2 || true
    exit 1
fi
log "Building audiocpp_server with $jobs jobs"
cmake --build "$build" --parallel "$jobs" --target audiocpp_server
[ -n "${CCACHE_DIR:-}" ] && ccache --show-stats

# --- Bundle -------------------------------------------------------------------------------
log "Bundling into $out"
mkdir -p "$out"
install -m 0755 "$build/bin/audiocpp_server" "$out/audiocpp_server"
strip --strip-debug "$out/audiocpp_server"
install -m 0644 "$src/LICENSE" "$out/LICENSE"
cp -r "$src/model_specs" "$out/model_specs"
# The source changes this build carries, so the bundle says exactly what differs from upstream.
mkdir -p "$out/patches"
for p in "${patches[@]}"; do install -m 0644 "$p" "$out/patches/"; done

# Licenses of the third-party code compiled into the binary.
mkdir -p "$out/licenses"
(cd "$src/external" && find . -maxdepth 4 -type f \
    \( -iname 'licen[cs]e*' -o -iname 'copying*' -o -iname 'notice*' \) \
    -exec cp --parents {} "$out/licenses/" \;)
boringssl=$(find "$build/_deps" -maxdepth 1 -type d -name 'audiocpp_boringssl-src' | head -n1)
if [ -n "$boringssl" ]; then
    mkdir -p "$out/licenses/boringssl" && cp "$boringssl/LICENSE" "$out/licenses/boringssl/"
fi
if [ "$variant" = vulkan ]; then
    cp -r "$tools/licenses/." "$out/licenses/"
fi
cat >"$out/licenses/README.txt" <<'EOF'
Licenses of the third-party code compiled into audiocpp_server, copied from the audio.cpp
source tree (external/) and the libraries its build fetches by pinned checksum.
llama_tokenizer has no license file of its own: it comes from llama.cpp, by the ggml
authors, under the MIT license in ggml/LICENSE. The GCC runtime libraries linked in
(libstdc++, libgcc, libgomp) are covered by the GCC Runtime Library Exception.
EOF

# Assigned first, so a server that doesn't start fails the build here (set -e).
version=$("${server[@]}" --version)
patch_list="          none"
if [ ${#patches[@]} -gt 0 ]; then
    patch_list=$(cd "$patch_dir" && sha256sum "${patches[@]##*/}" |
        awk '{print "          " $2 " (sha256 " $1 ")"}')
fi
cat >"$out/BUILD-INFO.txt" <<EOF
audio.cpp $AUDIOCPP_VERSION ($variant), built for Tomecast.

Source:   https://github.com/0xShug0/audio.cpp at commit $AUDIOCPP_COMMIT (tag v$AUDIOCPP_VERSION)
Patches:  Tomecast's own, all opt-in through environment variables (see patches/):
$patch_list
Built by: ${BUILD_URL:-a local run of .github/scripts/build-audiocpp.sh}
Base:     $(grep -oP "(?<=^PRETTY_NAME=\").*(?=\")" /etc/os-release), $(ldd --version | head -n1)
Compiler: $(gcc --version | head -n1)
Options:  ${cmake_args[*]}

$version
EOF
cat "$out/BUILD-INFO.txt"

# --- Check that it really is portable ------------------------------------------------------
log "Checking the binary"
bin="$out/audiocpp_server"
readelf -d "$bin" | grep -E 'NEEDED|RPATH|RUNPATH'
allowed='^(libc|libm|libdl|libpthread|librt|ld-linux-x86-64)\.so\.[0-9]+$'
[ "$variant" = vulkan ] && allowed='^(libc|libm|libdl|libpthread|librt|ld-linux-x86-64|libvulkan)\.so\.[0-9]+$'
needed=$(readelf -d "$bin" | sed -n 's/.*Shared library: \[\(.*\)\]/\1/p')
bad=$(echo "$needed" | grep -Ev "$allowed" || true)
if [ -n "$bad" ]; then
    echo "audiocpp_server needs libraries that aren't on every system: $bad" >&2
    exit 1
fi
if readelf -d "$bin" | grep -qE 'RPATH|RUNPATH'; then
    echo "audiocpp_server has a library search path; it shouldn't need one" >&2
    exit 1
fi
glibc=$(objdump -T "$bin" | grep -oE 'GLIBC_[0-9]+\.[0-9]+(\.[0-9]+)?' | sort -uV | tail -n1)
echo "Newest glibc symbol version needed: $glibc"
if [ "$(printf '%s\n' "$glibc" GLIBC_2.28 | sort -V | tail -n1)" != GLIBC_2.28 ]; then
    echo "audiocpp_server needs $glibc, newer than this base system's 2.28" >&2
    exit 1
fi
if objdump -T "$bin" | grep -qE '\((GLIBCXX|CXXABI|GCC|GOMP|OMP)_'; then
    echo "audiocpp_server still needs a shared GCC runtime library" >&2
    exit 1
fi
# Each patch's code is really in the server, not just applied to a file the build skipped.
for p in "${patches[@]}"; do
    read -ra expected <<<"$(sed -n 's/^Binary-contains: //p' "$p" | tr '\n' ' ')"
    if [ "${#expected[@]}" -eq 0 ]; then
        echo "$(basename "$p") lists nothing to check under Binary-contains:" >&2
        exit 1
    fi
    for s in "${expected[@]}"; do
        if ! grep -qaF "$s" "$bin"; then
            echo "audiocpp_server doesn't contain \"$s\", so $(basename "$p") isn't compiled in" >&2
            exit 1
        fi
    done
    echo "$(basename "$p"): compiled in (found ${expected[*]})"
done
rm -rf "$work"
log "Done: $(du -h "$bin" | cut -f1) audiocpp_server, needs $glibc"
