# AcousticBrainz-ng

This plugin is not affiliated with the [AcousticBrainz](https://acousticbrainz.org) project.

## Note

This branch targets Picard 3 (plugin API v3). For Picard 2, use the [`apiv2`](https://github.com/cy1der/acousticbrainz-ng/tree/apiv2) branch.

The build scripts for macOS and Windows haven't been tested yet. Earlier versions of the plugin crashed on Windows with an access violation (exit code 3221225477), if you run into this or have a fix, please let me know.

## Features

- Moods
- ReplayGain
- BPM
- Key
- [Tags](./models/msd-musicnn-1.json)

## Getting started

1. Install the plugin, either from Picard's plugin options page or with `picard-cli plugins install https://github.com/cy1der/acousticbrainz-ng`
2. Build the analysis tools, see below
3. Enable the plugin
4. If FFmpeg is not on your `PATH`, set the FFmpeg executable path in the plugin options page

Select albums, tracks, clusters or files and use **Plugins → AcousticBrainz-ng → Analyze** from the context menu. Results are cached, analyzing the same files again or matching them to tracks later applies the cached results without analyzing them again. Analyzing a single track reuses the album's ReplayGain if the whole album has been analyzed before.

Analysis results are cached in Picard's cache directory by default, this can be changed in the plugin options page.

Settings from the Picard 2 version of the plugin are migrated automatically, except for the binaries, models and cache paths.

## Building the analysis tools

The plugin uses a few small programs built on [Essentia](https://essentia.upf.edu) (sources in [`extractors/`](./extractors)), which need to be built for your system. The build scripts download all dependencies with verified checksums, so no other packages are needed. Run them in the plugin's install directory (`picard-cli plugins info AcousticBrainz-ng` shows it), they put the tools in `bin/<platform>/` where the plugin finds them automatically. When building somewhere else, set the binaries path in the plugin options page.

A build takes a few minutes and needs about 2 GB of disk space. The `build/` directory can be deleted afterwards, the tools in `bin/` use about 550 MB, most of which is TensorFlow.

### Linux and macOS

Requirements: a C/C++ compiler, `cmake`, `make`, `tar` and `curl` or `wget`. `nasm` is optional and makes decoding a bit faster on x86_64.

```sh
scripts/build-binaries.sh
```

Supported platforms are Linux x86_64 and macOS (Apple Silicon and Intel). There is no official TensorFlow C library for Linux on ARM, if you have one, pass it with `--tensorflow DIR`. See `scripts/build-binaries.sh --help` for all options.

The tools only depend on the C and C++ runtime libraries and the bundled TensorFlow library, so they keep working when your system's libraries are updated.

### Windows

Requirements: [Visual Studio 2022 or the Build Tools for Visual Studio](https://visualstudio.microsoft.com/downloads/) with the "Desktop development with C++" workload.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-binaries.ps1
```

## License

- The plugin is licensed under the GPL-2.0-or-later, see [`LICENSE`](./LICENSE).
- The tools in `extractors/` are based on Essentia's examples and, like Essentia, licensed under the AGPL-3.0-or-later, see [`extractors/LICENSE`](./extractors/LICENSE). The plugin only runs them as separate programs.
- The models in `models/` were created by the Music Technology Group of Universitat Pompeu Fabra and are licensed under the CC BY-NC-SA 4.0, see [`models/LICENSE.md`](./models/LICENSE.md). **They may not be used for commercial purposes.**

The built tools also include FFmpeg (LGPL-2.1-or-later), FFTW (GPL-2.0-or-later), Eigen (MPL-2.0), libyaml (MIT), libsamplerate (BSD-2-Clause) and TensorFlow (Apache-2.0). The build scripts put the license texts of all of them in `bin/<platform>/licenses/`, keep that directory when distributing the built tools.
