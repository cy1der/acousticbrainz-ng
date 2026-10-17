# AcousticBrainz-ng

This plugin is not affiliated with the [AcousticBrainz](https://acousticbrainz.org) project.

This branch targets Picard 3 (plugin API v3). For Picard 2, use the [`apiv2`](https://github.com/cy1der/acousticbrainz-ng/tree/apiv2) branch.

## Note

The analysis tools are built and tested on every supported platform by GitHub Actions, but the plugin itself hasn't been tested in Picard on macOS yet. If you have a Mac, please let me know if this plugin works on macOS or not.

## Features

- Moods
- ReplayGain
- BPM
- Key
- [Tags](./models/msd-musicnn-1.json)

## Getting started

1. Install the [latest release](https://github.com/cy1der/acousticbrainz-ng/releases/latest) of the plugin, either from Picard's plugin options page with `https://github.com/cy1der/acousticbrainz-ng` as the URL and `v3.2.1` as the ref/tag, or with `picard-cli plugins install https://github.com/cy1der/acousticbrainz-ng --ref v3.2.1`
2. Enable the plugin
3. Install the analysis tools, see below

Installed from a release tag, Picard only updates the plugin to newer releases. Installed without one, it follows the development branch instead. To switch an existing install to the releases, run `picard-cli plugins switch-ref AcousticBrainz-ng v3.2.1`.

Select albums, tracks, clusters or files and use **Plugins → AcousticBrainz-ng → Analyze** from the context menu. Results are cached, analyzing the same files again or matching them to tracks later applies the cached results without analyzing them again. Analyzing a single track reuses the album's ReplayGain if the whole album has been analyzed before.

To cancel a running analysis, use **Plugin Tools → AcousticBrainz-ng → Stop analysis** from the menu bar. Files already being analyzed are finished and saved to the cache, the rest of the queue is skipped.

Analysis results are cached in Picard's cache directory by default, this can be changed in the plugin options page.

Settings from the Picard 2 version of the plugin are migrated automatically, except for the binaries, models and cache paths.

## Analysis tools

The plugin uses a few small programs built on [Essentia](https://essentia.upf.edu) (sources in [`extractors/`](./extractors)), and FFmpeg for ReplayGain. The tools include audio-only `ffmpeg` and `ffprobe` programs, the plugin uses them unless the FFmpeg path is set in the plugin options page, and falls back to an FFmpeg on the `PATH`. They are versioned separately from the plugin since they rarely change, this version of the plugin needs the **`tools-2`** release.

The plugin looks for them in `bin/<platform>/` in its install directory, `picard-cli plugins info AcousticBrainz-ng` shows where that is. When they are somewhere else, set the binaries path in the plugin options page.

### Prebuilt

Click **Install tools** in the plugin options page (Options → Plugins → AcousticBrainz-ng). It downloads the tools for your platform from the [`tools-2` release](https://github.com/cy1der/acousticbrainz-ng/releases/tag/tools-2), checks them against the checksums pinned in the plugin and installs them. It uses Picard's proxy settings, only HTTP proxies are supported.

To install them manually instead, download the archive for your platform and extract it into the plugin's `bin/` directory, it contains a `<platform>/` directory:

| Platform | Archive |
|----------|---------|
| Linux x86_64 (glibc 2.28 or newer) | `abng-tools-2-linux-x86_64.tar.xz` |
| Linux ARM64 (glibc 2.28 or newer) | `abng-tools-2-linux-arm64.tar.xz` |
| macOS Apple Silicon (12 or newer) | `abng-tools-2-macos-arm64.tar.xz` |
| macOS Intel (12 or newer) | `abng-tools-2-macos-x86_64.tar.xz` |
| Windows x86_64, also Windows on ARM | `abng-tools-2-windows-x86_64.zip` |

The archives are built by GitHub Actions, check them with `sha256sum -c SHA256SUMS` or `gh attestation verify <archive> -R cy1der/acousticbrainz-ng`. After extracting, `python3 scripts/test-binaries.py bin/<platform>` in the plugin's install directory checks that they work.

### Building

The build scripts download all dependencies with verified checksums, so no other packages are needed. Run them in the plugin's install directory, they put the tools in `bin/<platform>/`.

A build takes a few minutes and needs about 2 GB of disk space. The `build/` directory can be deleted afterwards, the tools in `bin/` use about 550 MB, most of which is TensorFlow.

### Linux and macOS

Requirements: a C/C++ compiler, `cmake`, `make`, `tar` and `curl` or `wget`. `nasm` is optional and makes decoding a bit faster on x86_64.

```sh
scripts/build-binaries.sh
```

Supported platforms are Linux (x86_64 and ARM64) and macOS (Apple Silicon and Intel). Linux on ARM also needs `python3`, there is no official TensorFlow C library for it so the script takes the library from TensorFlow's Python package. See `scripts/build-binaries.sh --help` for all options.

The tools only depend on the C and C++ runtime libraries and the bundled TensorFlow library, so they keep working when your system's libraries are updated.

### Windows

Requirements: [Visual Studio 2022 or the Build Tools for Visual Studio](https://visualstudio.microsoft.com/downloads/) with the "Desktop development with C++" workload.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-binaries.ps1
```

This uses prebuilt FFmpeg libraries and doesn't include the `ffmpeg` and `ffprobe` programs, install FFmpeg separately then. To include them, build FFmpeg first in an [MSYS2](https://www.msys2.org) shell started from the Visual Studio developer command prompt, with `make`, `diffutils` and `nasm` installed and the `MSYS2_PATH_TYPE=inherit` environment variable set:

```sh
scripts/build-binaries.sh --ffmpeg-only
```

Then pass it to the build script with `-FFmpeg build\prefix-windows-x86_64`.

### Releasing

Publish a new tools release when `extractors/` or the dependencies in the build scripts change:

1. Run the **Analysis tools** workflow from the Actions tab with the next release number, e.g. `2`. It builds and tests the tools on every platform, then publishes the `tools-2` release with checksums and build provenance. Release numbers are plain integers, Picard would mistake tags like `tools-v2.0.0` for plugin versions.
2. Update the release number in this README and in `TOOLS_RELEASE` in `constants.py`, and copy the checksums from the release's `SHA256SUMS` to `TOOLS_CHECKSUMS` in `constants.py`. The install button only accepts archives matching these checksums.

Pull requests that change the tools run the same builds without publishing anything.

## Releasing the plugin

1. Update `version` in `MANIFEST.toml` and the release tag in "Getting started" above. If the tools changed, publish and pin a new tools release first, see above.
2. Check the release with `scripts/check-release.py v<version>`, then commit, tag the commit with `v<version>`, e.g. `v3.2.0`, and push the tag.

Pushing the tag publishes a GitHub release for it after running the same check. Tags have to look like `v1.2.3`, Picard ignores other tags when updating plugins installed from a tag.

## License

- The plugin is licensed under the GPL-2.0-or-later, see [`LICENSE`](./LICENSE).
- The tools in `extractors/` are based on Essentia's examples and, like Essentia, licensed under the AGPL-3.0-or-later, see [`extractors/LICENSE`](./extractors/LICENSE). The plugin only runs them as separate programs.
- The models in `models/` were created by the Music Technology Group of Universitat Pompeu Fabra and are licensed under the CC BY-NC-SA 4.0, see [`models/LICENSE.md`](./models/LICENSE.md). **They may not be used for commercial purposes.**

The built tools also include FFmpeg (LGPL-2.1-or-later, also the `ffmpeg` and `ffprobe` programs), FFTW (GPL-2.0-or-later), Eigen (MPL-2.0), libyaml (MIT), libsamplerate (BSD-2-Clause) and TensorFlow (Apache-2.0). The build scripts put the license texts of all of them in `bin/<platform>/licenses/`, keep that directory when distributing the built tools.
