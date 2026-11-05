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
#   SOURCE   a checkout of https://github.com/0xShug0/audio.cpp at $AUDIOCPP_COMMIT
#   OUTPUT   an empty folder for the bundle: audiocpp_server, LICENSE, model_specs/,
#            licenses/ and BUILD-INFO.txt (the official tarball's layout, minus the tools
#            Tomecast doesn't use)
#
# The audio.cpp source is built unmodified, with upstream's release options. The only
# differences from upstream's Linux release build are about where the result runs:
#   - it's built against glibc 2.28 instead of 2.43;
#   - libstdc++, libgcc and libgomp (OpenMP) are linked in statically, so the server only
#     needs glibc's own libraries, plus the system's libvulkan.so.1 for the Vulkan variant;
#   - the CPU kernels target AVX2-era CPUs (x86-64-v3) instead of whichever CPU the build
#     machine happened to have (-march=native).
#
# Optional environment: CCACHE_DIR (use ccache), VULKAN_TOOLS (where to keep the Vulkan
# headers, loader and glslc built here, so CI can cache them), BUILD_URL (for BUILD-INFO).
set -euo pipefail

variant=${1:?usage: build-audiocpp.sh cpu|vulkan SOURCE OUTPUT}
src=$(realpath "${2:?missing SOURCE}")
out=${3:?missing OUTPUT}
: "${AUDIOCPP_VERSION:?set AUDIOCPP_VERSION, e.g. 0.9.4}"
: "${AUDIOCPP_COMMIT:?set AUDIOCPP_COMMIT to the full commit SHA of that release}"
case "$variant" in cpu | vulkan) ;; *) echo "unknown variant: $variant" >&2 && exit 2 ;; esac
jobs=$(nproc)
work=$(mktemp -d)
# The image's Pythons live in /opt/python; some build steps need a python3 on PATH.
command -v python3 >/dev/null || export PATH="/opt/python/cp312-cp312/bin:$PATH"

# --- Pinned sources for the Vulkan build tools (only the Vulkan variant needs them) -------
# Vulkan headers and loader from the same SDK release upstream's Windows build uses, and
# shaderc's glslc (with the exact glslang/SPIRV-Tools/SPIRV-Headers revisions its DEPS file
# names), built here because no prebuilt glslc runs on glibc 2.28.
VULKAN_SDK_TAG=vulkan-sdk-1.4.357.0
SHADERC_COMMIT=b64fd42c4a2a9d501c47aca5d3571f7673a5e9cc # v2026.4
GLSLANG_COMMIT=e1b562a8bed273a02f30b59b66a5d499793cede5
SPIRV_TOOLS_COMMIT=ef96ed763b43b59b33b31b362f09a02b729fa1c9
SPIRV_HEADERS_COMMIT=04fd3caa1e8267e4d95c806cad901181728e1006
VULKAN_SOURCES=(
    "vulkan-headers https://github.com/KhronosGroup/Vulkan-Headers/archive/refs/tags/$VULKAN_SDK_TAG.tar.gz e87dce08116151f6b6d7de6b6faf41498e87e6cf848ff16fa3bd5402190ad4a3"
    "vulkan-loader https://github.com/KhronosGroup/Vulkan-Loader/archive/refs/tags/$VULKAN_SDK_TAG.tar.gz 54f2537df22313768da0317dda2abdaaab7711b4081c48c869a79db343d0ae70"
    "shaderc https://github.com/google/shaderc/archive/$SHADERC_COMMIT.tar.gz b0f73c2f50df5f24e78fae6d04f1e95131c65ea3ea13fd8998a317a5d632a51f"
    "shaderc/third_party/glslang https://github.com/KhronosGroup/glslang/archive/$GLSLANG_COMMIT.tar.gz 907174a24713c6202c146f164bf81783f1fbc79c8cb821a30f18f159eb980312"
    "shaderc/third_party/spirv-tools https://github.com/KhronosGroup/SPIRV-Tools/archive/$SPIRV_TOOLS_COMMIT.tar.gz 82c62146083fd558735a3171cf97cfc47903ca7d368482e87f94bd44883c0f00"
    "shaderc/third_party/spirv-headers https://github.com/KhronosGroup/SPIRV-Headers/archive/$SPIRV_HEADERS_COMMIT.tar.gz 392f4801409aad9c4f1b77745f179952fd6264e4c8bd0fc1bc45dfa6807bf6d0"
)
# Changes whenever any pinned Vulkan source does, so a cached toolchain is never reused stale.
VULKAN_TOOLS_ID=$(printf '%s\n' "${VULKAN_SOURCES[@]}" | sha256sum | cut -c1-16)

log() { printf '\n=== %s\n' "$*"; }

# fetch URL SHA256 FOLDER: download a pinned source archive, check it, and unpack it.
fetch() {
    local archive="$work/download.tar.gz"
    curl -fsSL --retry 3 -o "$archive" "$1"
    echo "$2  $archive" | sha256sum --check --quiet
    mkdir -p "$3"
    tar -xzf "$archive" --strip-components=1 -C "$3"
    rm "$archive"
}

build_vulkan_tools() {
    local prefix=$1 vsrc="$work/vulkan-src" name url sha
    log "Building the Vulkan headers, loader and glslc into $prefix"
    for entry in "${VULKAN_SOURCES[@]}"; do
        read -r name url sha <<<"$entry"
        fetch "$url" "$sha" "$vsrc/$name"
    done
    mkdir -p "$prefix" && find "$prefix" -mindepth 1 -delete # may be a mount point
    cmake -S "$vsrc/vulkan-headers" -B "$work/build-headers" -DCMAKE_INSTALL_PREFIX="$prefix"
    cmake --install "$work/build-headers"
    # Only linked against at build time: at run time the system's own libvulkan.so.1 is used.
    cmake -S "$vsrc/vulkan-loader" -B "$work/build-loader" -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$prefix" -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_PREFIX_PATH="$prefix" \
        -DBUILD_WSI_XCB_SUPPORT=OFF -DBUILD_WSI_XLIB_SUPPORT=OFF \
        -DBUILD_WSI_XLIB_XRANDR_SUPPORT=OFF -DBUILD_WSI_WAYLAND_SUPPORT=OFF -DBUILD_TESTS=OFF
    cmake --build "$work/build-loader" --parallel "$jobs"
    cmake --install "$work/build-loader"
    cmake -S "$vsrc/shaderc" -B "$work/build-shaderc" -DCMAKE_BUILD_TYPE=Release \
        -DSHADERC_SKIP_TESTS=ON -DSHADERC_SKIP_EXAMPLES=ON -DSHADERC_SKIP_COPYRIGHT_CHECK=ON \
        -DSHADERC_SKIP_INSTALL=ON -DSPIRV_SKIP_TESTS=ON -DSPIRV_SKIP_EXECUTABLES=ON \
        -DSPIRV_WERROR=OFF -DENABLE_GLSLANG_BINARIES=OFF
    cmake --build "$work/build-shaderc" --parallel "$jobs" --target glslc_exe
    install -D -m 0755 "$work/build-shaderc/glslc/glslc" "$prefix/bin/glslc"
    # ggml-vulkan includes <spirv/unified1/spirv.hpp>.
    cp -r "$vsrc/shaderc/third_party/spirv-headers/include/spirv" "$prefix/include/"
    mkdir -p "$prefix/licenses/vulkan-headers" "$prefix/licenses/spirv-headers"
    cp "$vsrc/vulkan-headers/LICENSE.md" "$prefix/licenses/vulkan-headers/"
    cp "$vsrc/shaderc/third_party/spirv-headers/LICENSE" "$prefix/licenses/spirv-headers/"
    echo "$VULKAN_TOOLS_ID" >"$prefix/.tools-id"
}

# --- Check the source ---------------------------------------------------------------------
# The checkout belongs to the runner's user; let git read it, so --version shows the commit.
git config --global --add safe.directory "$src"
actual=$(git -C "$src" rev-parse HEAD)
if [ "$actual" != "$AUDIOCPP_COMMIT" ]; then
    echo "expected audio.cpp at $AUDIOCPP_COMMIT, found $actual" >&2
    exit 1
fi
if [ -n "$(git -C "$src" status --porcelain)" ]; then
    echo "the audio.cpp checkout has local changes; refusing to build" >&2
    exit 1
fi

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
)
if [ "$variant" = vulkan ]; then
    tools=${VULKAN_TOOLS:-$work/vulkan-tools}
    if [ "$(cat "$tools/.tools-id" 2>/dev/null)" != "$VULKAN_TOOLS_ID" ]; then
        build_vulkan_tools "$tools"
    else
        log "Using the cached Vulkan tools in $tools"
    fi
    export VULKAN_SDK=$tools
    export PATH="$tools/bin:$PATH"
    cmake_args+=(-DENGINE_ENABLE_VULKAN=ON)
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

cat >"$out/BUILD-INFO.txt" <<EOF
audio.cpp $AUDIOCPP_VERSION ($variant), an unmodified build for Tomecast.

Source:   https://github.com/0xShug0/audio.cpp at commit $AUDIOCPP_COMMIT (tag v$AUDIOCPP_VERSION)
Built by: ${BUILD_URL:-a local run of .github/scripts/build-audiocpp.sh}
Base:     $(grep -oP "(?<=^PRETTY_NAME=\").*(?=\")" /etc/os-release), $(ldd --version | head -n1)
Compiler: $(gcc --version | head -n1)
Options:  ${cmake_args[*]}

$("$out/audiocpp_server" --version)
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
rm -rf "$work"
log "Done: $(du -h "$bin" | cut -f1) audiocpp_server, needs $glibc"
