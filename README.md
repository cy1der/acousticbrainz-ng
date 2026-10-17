# AcousticBrainz-ng

Analyzes your music in [MusicBrainz Picard](https://picard.musicbrainz.org) with [Essentia](https://essentia.upf.edu): moods, BPM, key, [tags](./models/msd-musicnn-1.json) and ReplayGain. Not affiliated with the [AcousticBrainz](https://acousticbrainz.org) project.

This branch is for Picard 3. For Picard 2, use the [`apiv2`](https://github.com/cy1der/acousticbrainz-ng/tree/apiv2) branch.

## Getting started

1. Install the plugin from Picard's plugin options with `https://github.com/cy1der/acousticbrainz-ng` as the URL and `v3.3.1` as the ref, or with `picard-cli plugins install https://github.com/cy1der/acousticbrainz-ng --ref v3.3.1`
2. Enable it, open its options page (**Options → Plugins → AcousticBrainz-ng**) and click **Install tools**
3. Select albums, tracks, clusters or files and use **Plugins → AcousticBrainz-ng → Analyze** from the context menu

Installed from a release tag, Picard updates the plugin to newer releases. An install that follows the development branch can be switched with `picard-cli plugins switch-ref AcousticBrainz-ng v3.3.1`.

## Usage

- Results are cached. Analyzing the same files again, or matching them to tracks later, applies the cached results right away. The cache is in Picard's cache directory unless set otherwise in the options.
- **Plugin Tools → AcousticBrainz-ng → Stop analysis** in the menu bar finishes the files being analyzed and skips the rest.
- Analyzing a single track reuses the album's ReplayGain if the whole album was analyzed before.
- Settings of the Picard 2 version are migrated, except the paths.

## GPU

Check **Use the GPU for MusicNN** in the options page and click **Install GPU tools** (on macOS the regular tools already include it). The options page shows the GPUs it found, and **Check** next to the binaries path tests whether the tools really use one. When the GPU can't be used, the plugin analyzes on the CPU and logs why.

- **Windows:** any GPU with DirectX 12 (NVIDIA, AMD, Intel)
- **Linux:** any GPU with a Vulkan driver (NVIDIA, AMD, Intel)
- **macOS:** Apple Silicon and Intel Macs, through CoreML

With both an integrated and a discrete GPU, the **GPU** setting below the option chooses between them, on Windows and Linux. The discrete GPU is used by default.

Most of the analysis time is spent in the MusicNN models. For a 4-minute track they take about 9 s on an RTX 4060 laptop GPU and 15 s on a 16-thread laptop CPU. Integrated GPUs are about as fast as the CPU, but leave it free for the rest of the analysis.

## Compatibility

The analysis tools need Linux with glibc 2.28 or newer, macOS 14 or newer on Apple Silicon and 13.4 or newer on Intel Macs, or 64-bit Windows 10 or newer.

What has been tested with the current analysis tools (`tools-4`). ✅ works, ❌ broken, ❔ not tested yet. Please [open an issue](https://github.com/cy1der/acousticbrainz-ng/issues) with your results, especially for the ❔ rows.

| OS | Arch | Backend | GPU | Status | Notes |
|---------|-----------------------|--------|---------------------------|----|-------|
| Linux   | x86_64                | CPU    | —                         | ✅ | Ryzen 7 7840HS on Gentoo Linux |
| Linux   | x86_64                | WebGPU | NVIDIA                    | ✅ | RTX 4060 Laptop using 615.78.08 on Gentoo Linux |
| Linux   | x86_64                | WebGPU | AMD                       | ✅ | Integrated Radeon 780M on Gentoo Linux |
| Linux   | x86_64                | WebGPU | Intel                     | ❔ | |
| Linux   | ARM64                 | CPU    | —                         | ❔ | |
| Linux   | ARM64                 | WebGPU | any                       | ❔ | |
| macOS   | ARM64 (Apple Silicon) | CPU    | —                         | ❔ | |
| macOS   | ARM64 (Apple Silicon) | CoreML | Apple                     | ❔ | |
| macOS   | x86_64 (Intel)        | CPU    | —                         | ❔ | |
| macOS   | x86_64 (Intel)        | CoreML | AMD, Intel                | ❔ | |
| Windows | x86_64                | CPU    | —                         | ✅ | Ryzen 7 7840HS on Windows 11 |
| Windows | x86_64                | WebGPU | NVIDIA                    | ✅ | RTX 4060 Laptop using 610.88 on Windows 11 |
| Windows | x86_64                | WebGPU | AMD                       | ✅ | Integrated Radeon 780M on Windows 11 |
| Windows | x86_64                | WebGPU | Intel                     | ❔ | |
| Windows | ARM64 (x64 emulation) | CPU    | —                         | ❔ | |
| Windows | ARM64 (x64 emulation) | WebGPU | Qualcomm                  | ❔ | |

To test, analyze a few tracks, then click **Check** next to the binaries path, which tells whether the GPU is used. Or run `python3 scripts/test-binaries.py --require-gpu bin/<platform>` in the plugin directory.

## Analysis tools

The plugin runs a few small programs built on Essentia and [ONNX Runtime](https://onnxruntime.ai) (sources in [`extractors/`](./extractors)), and FFmpeg, which is included with them. **Install tools** downloads them from the [`tools-4` release](https://github.com/cy1der/acousticbrainz-ng/releases/tag/tools-4), checks them against the checksums pinned in the plugin and puts them in `bin/<platform>/` in the plugin directory (`picard-cli plugins info AcousticBrainz-ng` shows where that is). It uses Picard's proxy settings, only HTTP proxies are supported.

To install them manually, extract the archive for your platform from the release into the plugin's `bin/` directory. The `-gpu` archives include GPU support for Linux and Windows.

To build them yourself, run `scripts/build-binaries.sh` (Linux, macOS) or `scripts\build-binaries.ps1` (Windows) in the plugin directory, add `--webgpu` / `-WebGPU` for GPU support. See [CONTRIBUTING.md](./CONTRIBUTING.md) for the requirements.

## License

- The plugin is licensed under the GPL-2.0-or-later, see [`LICENSE`](./LICENSE).
- The tools in `extractors/` are based on Essentia's examples and, like Essentia, licensed under the AGPL-3.0-or-later, see [`extractors/LICENSE`](./extractors/LICENSE). The plugin only runs them as separate programs.
- The models in `models/` were created by the Music Technology Group of Universitat Pompeu Fabra and converted to ONNX, they are licensed under the CC BY-NC-SA 4.0, see [`models/LICENSE.md`](./models/LICENSE.md). **They may not be used for commercial purposes.**

The built tools also include FFmpeg (LGPL-2.1-or-later), FFTW (GPL-2.0-or-later), Eigen (MPL-2.0), libyaml (MIT), libsamplerate (BSD-2-Clause) and ONNX Runtime (MIT). Their license texts are in `bin/<platform>/licenses/`, keep that directory when distributing the tools.
