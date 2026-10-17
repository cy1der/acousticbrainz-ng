#!/usr/bin/env bash
#
# Builds the Essentia based tools used by the plugin on Linux and macOS,
# together with audio-only ffmpeg and ffprobe programs, which the plugin uses
# for ReplayGain, hashing and downmixing.
#
# All dependencies except ONNX Runtime are built from source as static
# libraries, so no distribution packages are needed and the binaries keep
# working when the system's libraries are upgraded. The ONNX Runtime library is
# downloaded and placed next to the binaries.
#
# Requirements: a C/C++ compiler, cmake, make, tar, curl or wget. --webgpu
# also needs python3.
# Optional: nasm (faster FFmpeg decoders on x86_64).
#
# On Windows, build-binaries.ps1 builds the tools. In an MSYS2 shell with the
# Visual Studio environment, this script can build FFmpeg for it with
# --ffmpeg-only.

set -euo pipefail

ESSENTIA_COMMIT="c4db58ed928406982688c554344f34876dfef9cc"

# name|version|url|sha256
SOURCES=(
  "essentia|${ESSENTIA_COMMIT}|https://github.com/wo80/essentia/archive/${ESSENTIA_COMMIT}.tar.gz|506502695f63cfab7ad3bcd80e35fcf1a53ff392124db0f1d57c83f3aa0639a9"
  "eigen|5.0.1|https://gitlab.com/libeigen/eigen/-/archive/5.0.1/eigen-5.0.1.tar.gz|e9c326dc8c05cd1e044c71f30f1b2e34a6161a3b6ecf445d56b53ff1669e3dec"
  "fftw|3.3.11|https://www.fftw.org/fftw-3.3.11.tar.gz|5630c24cdeb33b131612f7eb4b1a9934234754f9f388ff8617458d0be6f239a1"
  "yaml|0.2.5|https://github.com/yaml/libyaml/archive/refs/tags/0.2.5.tar.gz|fa240dbf262be053f3898006d502d514936c818e422afdcf33921c63bed9bf2e"
  "libsamplerate|0.2.2|https://github.com/libsndfile/libsamplerate/archive/refs/tags/0.2.2.tar.gz|16e881487f184250deb4fcb60432d7556ab12cb58caea71ef23960aec6c0405a"
  "ffmpeg|9.0.2|https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.gz|84960df915059e8754fef2cd7c9afeb614062b1b5458ec471eecee619ee04e98"
)

# platform|version|url|sha256. 1.23.2 is the last release for Intel Macs. The
# macOS libraries include the CoreML execution provider
ONNXRUNTIME=(
  "linux-x86_64|1.31.0|https://github.com/microsoft/onnxruntime/releases/download/v1.31.0/onnxruntime-linux-x64-1.31.0.tgz|cc5c72baf5ae5c8238a6841f0897227be2d02826b9cf98eaf02fdefaeeb03a57"
  "linux-arm64|1.31.0|https://github.com/microsoft/onnxruntime/releases/download/v1.31.0/onnxruntime-linux-aarch64-1.31.0.tgz|c5b8b3cca31f3d643a3b313b8f42e2d0f16a4fcefd02cda343d97bd5306afaab"
  "macos-arm64|1.31.0|https://github.com/microsoft/onnxruntime/releases/download/v1.31.0/onnxruntime-osx-arm64-1.31.0.tgz|1031b36dd3bdaa8d976a6130d9cf2a05e9be320b93ae804f8427b91dc3432afc"
  "macos-x86_64|1.23.2|https://github.com/microsoft/onnxruntime/releases/download/v1.23.2/onnxruntime-osx-x86_64-1.23.2.tgz|d10359e16347b57d9959f7e80a225a5b4a66ed7d7e007274a15cae86836485a6"
)

# The WebGPU execution provider (Vulkan), installed with --webgpu for the GPU
# builds. platform|version|url|sha256 of the Python package, which contains the
# plugin library
WEBGPU_EP=(
  "linux-x86_64|0.4.0|https://files.pythonhosted.org/packages/c1/96/2a18a45079250afcd825687aef2895de266d5abc1cff9f52d2dede7598f2/onnxruntime_ep_webgpu-0.4.0-py3-none-manylinux_2_28_x86_64.whl|5d8c961eda91a88961cad1fb7621f389f006b18497d6ea9ecf96f729f066cbfb"
  "linux-arm64|0.4.0|https://files.pythonhosted.org/packages/4e/ca/00c70322c19913c81a6bb2239aca83781b97a818b55c2ec3d25cec72dc4c/onnxruntime_ep_webgpu-0.4.0-py3-none-manylinux_2_28_aarch64.whl|17f660db53b1a509c63e721ac6784a2daa1c4e91a968523ee9986505ee5b0a55"
)

# Audio formats supported by Picard
FFMPEG_DEMUXERS="aac,ac3,aiff,ape,asf,au,caf,dsf,dts,eac3,flac,iff,matroska,mlp,mov,mp3,mpc,mpc8,ogg,tak,truehd,tta,w64,wav,wv"
FFMPEG_DECODERS="aac,aac_latm,ac3,alac,ape,dca,dsd_lsbf,dsd_lsbf_planar,dsd_msbf,dsd_msbf_planar,eac3,flac,mlp,mp1,mp1float,mp2,mp2float,mp3,mp3float,mpc7,mpc8,opus,pcm_*,speex,tak,truehd,tta,vorbis,wavpack,wmalossless,wmapro,wmav1,wmav2"
FFMPEG_PARSERS="aac,aac_latm,ac3,dca,flac,mlp,mpegaudio,opus,tak,vorbis"
# What the plugin's ffmpeg commands use: loudness measurement of single files
# and albums, hashing the audio packets and downmixing to WAV
FFMPEG_FILTERS="aformat,anull,aresample,concat,loudnorm"
FFMPEG_MUXERS="md5,null,wav"
FFMPEG_ENCODERS="pcm_f32le,pcm_s16le"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<EOF
Usage: $0 [options]

Builds abng_key, abng_musicnn and abng_rhythm and installs them together with
the ONNX Runtime library.

Options:
  --output DIR       Install directory (default: bin/<platform> in the repository)
  --work-dir DIR     Download and build directory (default: build/ in the repository)
  --onnxruntime DIR  Use an existing ONNX Runtime build (a directory containing
                     include/ and lib/) instead of downloading it
  --webgpu           Also install the WebGPU execution provider, which runs the
                     MusicNN models on the GPU (Linux only, macOS uses CoreML)
  --jobs N          Number of parallel build jobs (default: number of CPUs)
  --ffmpeg-only      Only build FFmpeg into the work directory, used to build
                     it for build-binaries.ps1 on Windows
  --clean            Remove the work directory before building
  -h, --help         Show this help
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

log() {
  echo "==> $*"
}

case "$(uname -s)" in
  Linux) OS=linux ;;
  Darwin) OS=macos ;;
  MINGW*|MSYS*) OS=windows ;;
  *) die "Unsupported operating system $(uname -s)" ;;
esac

case "$(uname -m)" in
  x86_64|amd64) ARCH=x86_64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) die "Unsupported architecture $(uname -m)" ;;
esac

PLATFORM="$OS-$ARCH"

if [ "$OS" = macos ]; then
  JOBS="$(sysctl -n hw.ncpu)"
else
  JOBS="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)"
fi

OUTPUT_DIR="$REPO_DIR/bin/$PLATFORM"
WORK_DIR="$REPO_DIR/build"
ONNXRUNTIME_DIR=""
WEBGPU=0
CLEAN=0
FFMPEG_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --output) OUTPUT_DIR="$2"; shift 2 ;;
    --work-dir) WORK_DIR="$2"; shift 2 ;;
    --onnxruntime) ONNXRUNTIME_DIR="$2"; shift 2 ;;
    --webgpu) WEBGPU=1; shift ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --ffmpeg-only) FFMPEG_ONLY=1; shift ;;
    --clean) CLEAN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "Unknown option $1" ;;
  esac
done

if [ "$OS" = windows ]; then
  [ "$FFMPEG_ONLY" = 1 ] || die "Use build-binaries.ps1 on Windows, this script only builds FFmpeg there (--ffmpeg-only)"
  command -v cl >/dev/null 2>&1 || die "The Visual Studio compiler (cl) isn't on the PATH, set up the Visual Studio environment first"
  for tool in make tar; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
  done
else
  for tool in cmake make tar; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
  done
  command -v "${CC:-cc}" >/dev/null 2>&1 || die "A C compiler is required (set CC to use a different one)"
  command -v "${CXX:-c++}" >/dev/null 2>&1 || die "A C++ compiler is required (set CXX to use a different one)"
fi
command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || die "curl or wget is required"

if [ "$WEBGPU" = 1 ] && [ "$OS" != linux ]; then
  die "--webgpu is only supported on Linux, macOS uses CoreML"
fi

if [ "$CLEAN" = 1 ]; then
  rm -rf "$WORK_DIR"
fi

DOWNLOAD_DIR="$WORK_DIR/downloads"
SOURCE_DIR="$WORK_DIR/src"
BUILD_DIR="$WORK_DIR/build-$PLATFORM"
PREFIX="$WORK_DIR/prefix-$PLATFORM"
mkdir -p "$DOWNLOAD_DIR" "$SOURCE_DIR" "$BUILD_DIR" "$PREFIX/stamps"

# Never let the dependency builds pick up the system's libraries
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
export CMAKE_BUILD_PARALLEL_LEVEL="$JOBS"

if [ "$OS" = macos ]; then
  # The minimum versions of the ONNX Runtime libraries
  if [ "$ARCH" = arm64 ]; then
    export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-14.0}"
  else
    export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.4}"
  fi
fi

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# fetch <file name> <url> <sha256>
fetch() {
  local file="$DOWNLOAD_DIR/$1"
  if [ ! -f "$file" ]; then
    log "Downloading $1"
    if command -v curl >/dev/null 2>&1; then
      curl -fL --retry 3 -o "$file.part" "$2"
    else
      wget -O "$file.part" "$2"
    fi
    mv "$file.part" "$file"
  fi

  local actual
  actual="$(sha256 "$file")"
  if [ "$actual" != "$3" ]; then
    rm -f "$file"
    die "Checksum mismatch for $1 (expected $3, got $actual), removed the file"
  fi
}

# unpack <name> <version>: downloads and extracts a source archive, prints its directory
unpack() {
  local entry name version url checksum
  for entry in "${SOURCES[@]}"; do
    IFS='|' read -r name version url checksum <<<"$entry"
    if [ "$name" = "$1" ]; then
      local archive="$name-$version.tar.${url##*.tar.}"
      fetch "$archive" "$url" "$checksum" >&2
      local dir="$SOURCE_DIR/$name-$version"
      if [ ! -d "$dir" ]; then
        mkdir -p "$dir.tmp"
        tar -xf "$DOWNLOAD_DIR/$archive" -C "$dir.tmp" --strip-components 1
        mv "$dir.tmp" "$dir"
      fi
      echo "$dir"
      return
    fi
  done
  die "Unknown source $1"
}

version_of() {
  local entry name version rest
  for entry in "${SOURCES[@]}"; do
    IFS='|' read -r name version rest <<<"$entry"
    if [ "$name" = "$1" ]; then
      echo "$version"
      return
    fi
  done
}

# Runs a dependency build function unless it already completed for this
# version, and the configuration identified by the optional second argument
build_dependency() {
  local stamp
  stamp="$PREFIX/stamps/$1-$(version_of "$1")${2:+-$2}"
  if [ -f "$stamp" ]; then
    log "$1 already built"
    return
  fi
  log "Building $1"
  "build_$1"
  touch "$stamp"
}

cmake_dependency() {
  local src="$1"
  shift
  local build
  build="$BUILD_DIR/$(basename "$src")"
  rm -rf "$build"
  cmake -S "$src" -B "$build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_PREFIX_PATH="$PREFIX" \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DBUILD_SHARED_LIBS=OFF \
    -DBUILD_TESTING=OFF \
    "$@"
  cmake --build "$build"
  cmake --install "$build"
}

build_eigen() {
  cmake_dependency "$(unpack eigen)" \
    -DEIGEN_BUILD_DOC=OFF \
    -DEIGEN_BUILD_BLAS=OFF \
    -DEIGEN_BUILD_LAPACK=OFF \
    -DEIGEN_BUILD_DEMOS=OFF
}

build_fftw() {
  local src simd=()
  src="$(unpack fftw)"
  if [ "$ARCH" = x86_64 ]; then
    # FFTW checks at runtime which instruction sets the CPU supports
    simd=(--enable-sse2 --enable-avx --enable-avx2)
  else
    simd=(--enable-neon)
  fi
  local build="$BUILD_DIR/fftw"
  rm -rf "$build"
  mkdir -p "$build"
  (
    cd "$build"
    # FFTW's assertions include the source file names, a relative path to the
    # sources keeps the build directory (e.g. the user's home) out of the binaries
    "../../src/$(basename "$src")/configure" --prefix="$PREFIX" --libdir="$PREFIX/lib" \
      --enable-float --enable-static --disable-shared --disable-fortran \
      --disable-doc --with-pic "${simd[@]}"
    make -j"$JOBS"
    make install
  )
}

build_yaml() {
  cmake_dependency "$(unpack yaml)" \
    -DINSTALL_CMAKE_DIR=lib/cmake/yaml
}

build_libsamplerate() {
  cmake_dependency "$(unpack libsamplerate)" \
    -DLIBSAMPLERATE_EXAMPLES=OFF \
    -DLIBSAMPLERATE_INSTALL=ON
}

build_ffmpeg() {
  local src options=()
  src="$(unpack ffmpeg)"
  if [ "$OS" = windows ]; then
    # MSYS2 has its own link.exe, make sure the Visual Studio linker is used.
    # -MD: the same C runtime as Essentia
    PATH="$(dirname "$(command -v cl)"):$PATH"
    options=(--toolchain=msvc --target-os=win64 --arch=x86_64 --enable-w32threads --extra-cflags=-MD)
  else
    options=(--cc="${CC:-cc}" --cxx="${CXX:-c++}" --enable-pthreads --enable-pic)
  fi
  if [ "$ARCH" = x86_64 ] && ! command -v nasm >/dev/null 2>&1; then
    echo "nasm not found, building FFmpeg without x86 assembly optimizations"
    options+=(--disable-x86asm)
  fi
  local build="$BUILD_DIR/ffmpeg"
  rm -rf "$build"
  mkdir -p "$build"
  (
    cd "$build"
    "$src/configure" --prefix="$PREFIX" --libdir="$PREFIX/lib" \
      --enable-static --disable-shared \
      --disable-autodetect --disable-doc --disable-network \
      --disable-programs --enable-ffmpeg --enable-ffprobe \
      --disable-everything --disable-avdevice --disable-swscale \
      --enable-protocol=file,pipe \
      --enable-demuxer="$FFMPEG_DEMUXERS" \
      --enable-decoder="$FFMPEG_DECODERS" \
      --enable-parser="$FFMPEG_PARSERS" \
      --enable-filter="$FFMPEG_FILTERS" \
      --enable-muxer="$FFMPEG_MUXERS" \
      --enable-encoder="$FFMPEG_ENCODERS" \
      "${options[@]}"
    make -j"$JOBS"
    make install
  )

  # For the license notice of build-binaries.ps1
  local entry name version url rest
  for entry in "${SOURCES[@]}"; do
    IFS='|' read -r name version url rest <<<"$entry"
    if [ "$name" = ffmpeg ]; then
      echo "ffmpeg $version (LGPL-2.1-or-later): $url" > "$PREFIX/FFMPEG_SOURCE"
    fi
  done
}

# Extracts the plugin library and its licenses from the WebGPU execution
# provider's Python package
extract_webgpu_wheel() {
  command -v python3 >/dev/null 2>&1 || die "python3 is required for --webgpu"
  python3 -I - "$1" "$2" <<'EOF'
import os
import shutil
import sys
import zipfile

wheel, destination = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(wheel) as archive:
    for info in archive.infolist():
        name = os.path.basename(info.filename)
        if not info.is_dir() and (name.startswith("libonnxruntime_providers_webgpu.") or name in ("LICENSE", "ThirdPartyNotices.txt")):
            with archive.open(info) as source, open(os.path.join(destination, name), "wb") as output:
                shutil.copyfileobj(source, output)
EOF
  [ -n "$(ls "$2"/libonnxruntime_providers_webgpu.* 2>/dev/null)" ] || die "No WebGPU execution provider in $1"
}

# lookup <table name>: prints the entry of this platform in the table, if any
lookup() {
  local -n table="$1"
  local entry
  for entry in "${table[@]}"; do
    if [ "${entry%%|*}" = "$PLATFORM" ]; then
      echo "$entry"
    fi
  done
}

if [ "$FFMPEG_ONLY" = 0 ]; then
  build_dependency eigen
  build_dependency fftw
  build_dependency yaml
  build_dependency libsamplerate
fi
# Rebuilds FFmpeg when its configuration changes
build_dependency ffmpeg "$({ declare -f build_ffmpeg; echo "$FFMPEG_DEMUXERS $FFMPEG_DECODERS $FFMPEG_PARSERS $FFMPEG_FILTERS $FFMPEG_MUXERS $FFMPEG_ENCODERS"; } | cksum | cut -d' ' -f1)"

if [ "$FFMPEG_ONLY" = 1 ]; then
  log "Done, FFmpeg is in $PREFIX"
  exit 0
fi

if [ -z "$ONNXRUNTIME_DIR" ]; then
  onnxruntime_entry="$(lookup ONNXRUNTIME)"
  [ -n "$onnxruntime_entry" ] || die "There is no ONNX Runtime library for $PLATFORM, pass one with --onnxruntime"

  IFS='|' read -r _ onnxruntime_version onnxruntime_url onnxruntime_checksum <<<"$onnxruntime_entry"
  ONNXRUNTIME_DIR="$WORK_DIR/onnxruntime-$PLATFORM-$onnxruntime_version"
  if [ ! -d "$ONNXRUNTIME_DIR" ]; then
    fetch "onnxruntime-$PLATFORM-$onnxruntime_version.tgz" "$onnxruntime_url" "$onnxruntime_checksum"
    rm -rf "$ONNXRUNTIME_DIR.tmp"
    mkdir -p "$ONNXRUNTIME_DIR.tmp"
    # The archives contain a single directory, named ./onnxruntime-<platform>-<version>
    # in some of them, which --strip-components doesn't handle
    tar -xzf "$DOWNLOAD_DIR/onnxruntime-$PLATFORM-$onnxruntime_version.tgz" -C "$ONNXRUNTIME_DIR.tmp"
    mv "$ONNXRUNTIME_DIR.tmp"/onnxruntime-* "$ONNXRUNTIME_DIR"
    rm -rf "$ONNXRUNTIME_DIR.tmp"
  fi
fi

ONNXRUNTIME_DIR="$(cd "$ONNXRUNTIME_DIR" && pwd)"
if [ "$OS" = macos ]; then
  ONNXRUNTIME_LIBRARY="$ONNXRUNTIME_DIR/lib/libonnxruntime.dylib"
else
  ONNXRUNTIME_LIBRARY="$ONNXRUNTIME_DIR/lib/libonnxruntime.so"
fi
[ -f "$ONNXRUNTIME_DIR/include/onnxruntime_cxx_api.h" ] || die "No ONNX Runtime headers in $ONNXRUNTIME_DIR/include"
[ -e "$ONNXRUNTIME_LIBRARY" ] || die "No ONNX Runtime library at $ONNXRUNTIME_LIBRARY"

if [ "$WEBGPU" = 1 ]; then
  webgpu_entry="$(lookup WEBGPU_EP)"
  [ -n "$webgpu_entry" ] || die "There is no WebGPU execution provider for $PLATFORM"
  IFS='|' read -r _ webgpu_version webgpu_url webgpu_checksum <<<"$webgpu_entry"
  WEBGPU_DIR="$WORK_DIR/webgpu-$PLATFORM-$webgpu_version"
  if [ ! -d "$WEBGPU_DIR" ]; then
    fetch "onnxruntime-ep-webgpu-$PLATFORM-$webgpu_version.whl" "$webgpu_url" "$webgpu_checksum"
    rm -rf "$WEBGPU_DIR.tmp"
    mkdir -p "$WEBGPU_DIR.tmp"
    extract_webgpu_wheel "$DOWNLOAD_DIR/onnxruntime-ep-webgpu-$PLATFORM-$webgpu_version.whl" "$WEBGPU_DIR.tmp"
    mv "$WEBGPU_DIR.tmp" "$WEBGPU_DIR"
  fi
fi

log "Building tools"
EXTRACTORS_BUILD="$BUILD_DIR/extractors"
cmake -S "$REPO_DIR/extractors" -B "$EXTRACTORS_BUILD" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH="$PREFIX" \
  -DESSENTIA_SOURCE_DIR="$(unpack essentia)" \
  -DAVCODEC_INCLUDE_DIRS="$PREFIX/include" -DAVCODEC_LIBRARIES="$PREFIX/lib/libavcodec.a" \
  -DAVFORMAT_INCLUDE_DIRS="$PREFIX/include" -DAVFORMAT_LIBRARIES="$PREFIX/lib/libavformat.a" \
  -DAVUTIL_INCLUDE_DIRS="$PREFIX/include" -DAVUTIL_LIBRARIES="$PREFIX/lib/libavutil.a" \
  -DSWRESAMPLE_INCLUDE_DIRS="$PREFIX/include" -DSWRESAMPLE_LIBRARIES="$PREFIX/lib/libswresample.a" \
  -DFFTW3f_INCLUDE_DIR="$PREFIX/include" -DFFTW3f_LIBRARY="$PREFIX/lib/libfftw3f.a" \
  -Dyaml_INCLUDE_DIR="$PREFIX/include" -Dyaml_LIBRARY="$PREFIX/lib/libyaml.a" \
  -DSampleRate_INCLUDE_DIR="$PREFIX/include" -DSampleRate_LIBRARY="$PREFIX/lib/libsamplerate.a" \
  -DONNXRUNTIME_INCLUDE_DIR="$ONNXRUNTIME_DIR/include" -DONNXRUNTIME_LIBRARY="$ONNXRUNTIME_LIBRARY"
cmake --build "$EXTRACTORS_BUILD"

log "Installing to $OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"
cmake --install "$EXTRACTORS_BUILD" --component tools --prefix "$OUTPUT_DIR" --strip
for program in ffmpeg ffprobe; do
  rm -f "$OUTPUT_DIR/$program"
  cp "$PREFIX/bin/$program" "$OUTPUT_DIR/"
done

# Copy the ONNX Runtime library, keeping the versioned names the binaries link
# to. Remove the libraries of a previous build first, including the TensorFlow
# ones of builds before tools-3, and the WebGPU provider when building without it
rm -f "$OUTPUT_DIR"/libtensorflow* "$OUTPUT_DIR"/libonnxruntime* "$OUTPUT_DIR/LICENSE.tensorflow"
if [ "$OS" = macos ]; then
  # Only the name the binaries link to, e.g. @rpath/libonnxruntime.1.dylib. The
  # other names are copies of the library in some releases
  onnxruntime_name="$(basename "$(otool -D "$ONNXRUNTIME_LIBRARY" | tail -n 1)")"
  cp -L "$ONNXRUNTIME_DIR/lib/$onnxruntime_name" "$OUTPUT_DIR/"
else
  cp -P "$ONNXRUNTIME_DIR"/lib/libonnxruntime.so* "$OUTPUT_DIR/"
fi
if [ "$WEBGPU" = 1 ]; then
  cp "$WEBGPU_DIR"/libonnxruntime_providers_webgpu.* "$OUTPUT_DIR/"
fi

# The licenses of everything included in the binaries, needed when
# distributing them
LICENSE_DIR="$OUTPUT_DIR/licenses"
rm -rf "$LICENSE_DIR"
mkdir -p "$LICENSE_DIR"

# copy_licenses <component> <files...>: missing files are skipped, a
# user-supplied ONNX Runtime might not include them
copy_licenses() {
  local component="$1" file
  shift
  mkdir -p "$LICENSE_DIR/$component"
  for file in "$@"; do
    if [ -f "$file" ]; then
      cp "$file" "$LICENSE_DIR/$component/"
    fi
  done
}

license_of() {
  case "$1" in
    essentia) echo "AGPL-3.0-or-later" ;;
    eigen) echo "MPL-2.0" ;;
    fftw) echo "GPL-2.0-or-later" ;;
    yaml) echo "MIT" ;;
    libsamplerate) echo "BSD-2-Clause" ;;
    ffmpeg) echo "LGPL-2.1-or-later" ;;
  esac
}

copy_licenses abng-tools "$REPO_DIR/extractors/LICENSE"
copy_licenses essentia "$(unpack essentia)/COPYING.txt"
copy_licenses eigen "$(unpack eigen)"/COPYING.*
copy_licenses fftw "$(unpack fftw)/COPYING"
copy_licenses yaml "$(unpack yaml)/License"
copy_licenses libsamplerate "$(unpack libsamplerate)/COPYING"
copy_licenses ffmpeg "$(unpack ffmpeg)/COPYING.LGPLv2.1" "$(unpack ffmpeg)/LICENSE.md"
copy_licenses onnxruntime "$ONNXRUNTIME_DIR/LICENSE" "$ONNXRUNTIME_DIR/ThirdPartyNotices.txt"
if [ "$WEBGPU" = 1 ]; then
  copy_licenses onnxruntime-ep-webgpu "$WEBGPU_DIR/LICENSE" "$WEBGPU_DIR/ThirdPartyNotices.txt"
fi

{
  echo "abng_key, abng_musicnn and abng_rhythm are licensed under the AGPL-3.0-or-later,"
  echo "ffmpeg and ffprobe under the LGPL-2.1-or-later."
  echo "Their source code and the scripts used to build them are available at"
  echo "https://github.com/cy1der/acousticbrainz-ng (extractors/ and scripts/)."
  echo
  echo "They include the following components, whose license texts are in the"
  echo "directories next to this file:"
  echo
  for entry in "${SOURCES[@]}"; do
    IFS='|' read -r name version url _ <<<"$entry"
    echo "- $name $version ($(license_of "$name")): $url"
  done
  if [ -n "${onnxruntime_url:-}" ]; then
    echo "- onnxruntime $onnxruntime_version (MIT): $onnxruntime_url"
  else
    echo "- onnxruntime (MIT): provided by the user from $ONNXRUNTIME_DIR"
  fi
  if [ "$WEBGPU" = 1 ]; then
    echo "- onnxruntime-ep-webgpu $webgpu_version (MIT): $webgpu_url"
  fi
} > "$LICENSE_DIR/README.txt"

# Without arguments the tools print their usage, anything else means they
# can't be loaded, e.g. because of a missing library
"$OUTPUT_DIR/ffmpeg" -version >/dev/null || die "$OUTPUT_DIR/ffmpeg doesn't run"
"$OUTPUT_DIR/ffprobe" -version >/dev/null || die "$OUTPUT_DIR/ffprobe doesn't run"
for tool in abng_key abng_musicnn abng_rhythm; do
  output="$("$OUTPUT_DIR/$tool" 2>&1 || true)"
  case "$output" in
    *Usage:*) ;;
    *) echo "$output" >&2; die "$OUTPUT_DIR/$tool doesn't run" ;;
  esac
done

log "Done, the binaries are in $OUTPUT_DIR"
