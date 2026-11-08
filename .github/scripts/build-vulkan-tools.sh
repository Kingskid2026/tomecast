#!/usr/bin/env bash
# Build what audio.cpp's Vulkan backend needs at build time, from pinned sources, into PREFIX:
# the Vulkan headers, a Vulkan loader to link against, the SPIR-V headers, and glslc (the
# shader compiler). Runs in the manylinux_2_28 image, called by build-audiocpp.sh:
#
#   build-vulkan-tools.sh PREFIX
#
# None of this ends up in the engine bundle except the headers' code compiled into it: at run
# time the engine uses the computer's own libvulkan.so.1 and graphics driver. glslc is built
# here because no prebuilt glslc runs on glibc 2.28. CI caches PREFIX, keyed on this file.
set -euo pipefail

prefix=${1:?usage: build-vulkan-tools.sh PREFIX}
jobs=$(nproc)
work=$(mktemp -d)

# The Vulkan headers and loader from the SDK release upstream's Windows build uses, and
# shaderc v2026.4 with the glslang, SPIRV-Tools and SPIRV-Headers revisions its DEPS file names.
SDK=vulkan-sdk-1.4.357.0
SOURCES=(
    "vulkan-headers https://github.com/KhronosGroup/Vulkan-Headers/archive/refs/tags/$SDK.tar.gz e87dce08116151f6b6d7de6b6faf41498e87e6cf848ff16fa3bd5402190ad4a3"
    "vulkan-loader https://github.com/KhronosGroup/Vulkan-Loader/archive/refs/tags/$SDK.tar.gz 54f2537df22313768da0317dda2abdaaab7711b4081c48c869a79db343d0ae70"
    "shaderc https://github.com/google/shaderc/archive/b64fd42c4a2a9d501c47aca5d3571f7673a5e9cc.tar.gz b0f73c2f50df5f24e78fae6d04f1e95131c65ea3ea13fd8998a317a5d632a51f"
    "shaderc/third_party/glslang https://github.com/KhronosGroup/glslang/archive/e1b562a8bed273a02f30b59b66a5d499793cede5.tar.gz 907174a24713c6202c146f164bf81783f1fbc79c8cb821a30f18f159eb980312"
    "shaderc/third_party/spirv-tools https://github.com/KhronosGroup/SPIRV-Tools/archive/ef96ed763b43b59b33b31b362f09a02b729fa1c9.tar.gz 82c62146083fd558735a3171cf97cfc47903ca7d368482e87f94bd44883c0f00"
    "shaderc/third_party/spirv-headers https://github.com/KhronosGroup/SPIRV-Headers/archive/04fd3caa1e8267e4d95c806cad901181728e1006.tar.gz 392f4801409aad9c4f1b77745f179952fd6264e4c8bd0fc1bc45dfa6807bf6d0"
)

src="$work/src"
for entry in "${SOURCES[@]}"; do
    read -r name url sha <<<"$entry"
    curl -fsSL --retry 3 -o "$work/download.tar.gz" "$url"
    echo "$sha  $work/download.tar.gz" | sha256sum --check --quiet
    mkdir -p "$src/$name"
    tar -xzf "$work/download.tar.gz" --strip-components=1 -C "$src/$name"
done

mkdir -p "$prefix" && find "$prefix" -mindepth 1 -delete # it may be a mount point
cmake -S "$src/vulkan-headers" -B "$work/build-headers" -DCMAKE_INSTALL_PREFIX="$prefix"
cmake --install "$work/build-headers"
cmake -S "$src/vulkan-loader" -B "$work/build-loader" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$prefix" -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_PREFIX_PATH="$prefix" \
    -DBUILD_WSI_XCB_SUPPORT=OFF -DBUILD_WSI_XLIB_SUPPORT=OFF \
    -DBUILD_WSI_XLIB_XRANDR_SUPPORT=OFF -DBUILD_WSI_WAYLAND_SUPPORT=OFF -DBUILD_TESTS=OFF
cmake --build "$work/build-loader" --parallel "$jobs"
cmake --install "$work/build-loader"
cmake -S "$src/shaderc" -B "$work/build-shaderc" -DCMAKE_BUILD_TYPE=Release \
    -DSHADERC_SKIP_TESTS=ON -DSHADERC_SKIP_EXAMPLES=ON -DSHADERC_SKIP_COPYRIGHT_CHECK=ON \
    -DSPIRV_SKIP_TESTS=ON -DSPIRV_SKIP_EXECUTABLES=ON -DSPIRV_WERROR=OFF \
    -DENABLE_GLSLANG_BINARIES=OFF
cmake --build "$work/build-shaderc" --parallel "$jobs" --target glslc_exe
install -D -m 0755 "$work/build-shaderc/glslc/glslc" "$prefix/bin/glslc"
# ggml-vulkan includes <spirv/unified1/spirv.hpp>.
cp -r "$src/shaderc/third_party/spirv-headers/include/spirv" "$prefix/include/"
# Code from these headers is compiled into the engine, so their licenses go in its bundle.
mkdir -p "$prefix/licenses/vulkan-headers" "$prefix/licenses/spirv-headers"
cp "$src/vulkan-headers/LICENSE.md" "$prefix/licenses/vulkan-headers/"
cp "$src/shaderc/third_party/spirv-headers/LICENSE" "$prefix/licenses/spirv-headers/"

sha256sum "$0" | cut -d' ' -f1 >"$prefix/.built-by"
rm -rf "$work"
"$prefix/bin/glslc" --version
