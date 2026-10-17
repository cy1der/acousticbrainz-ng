# Contributing

## Building the analysis tools

The build scripts download all dependencies with verified checksums, so no other packages are needed. Run them in the repository or the plugin's install directory, they put the tools in `bin/<platform>/`. A build takes a few minutes and needs about 2 GB of disk space. The `build/` directory can be deleted afterwards, the tools use about 90 MB.

The tools only depend on the C and C++ runtime libraries and the bundled ONNX Runtime library, so they keep working when the system's libraries are updated.

### Linux and macOS

Requirements: a C/C++ compiler, `cmake`, `make`, `tar` and `curl` or `wget`. `nasm` is optional and makes decoding a bit faster on x86_64.

```sh
scripts/build-binaries.sh
```

`--webgpu` (Linux only, also needs `python3`) adds the WebGPU execution provider, which runs the MusicNN models on the GPU. On macOS the tools always include CoreML. See `scripts/build-binaries.sh --help` for all options.

### Windows

Requirements: [Visual Studio 2022 or the Build Tools for Visual Studio](https://visualstudio.microsoft.com/downloads/) with the "Desktop development with C++" workload.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-binaries.ps1
```

`-WebGPU` adds the WebGPU execution provider.

This uses prebuilt FFmpeg libraries and doesn't include the `ffmpeg` and `ffprobe` programs, install FFmpeg separately then. To include them, build FFmpeg first in an [MSYS2](https://www.msys2.org) shell started from the Visual Studio developer command prompt, with `make`, `diffutils` and `nasm` installed and the `MSYS2_PATH_TYPE=inherit` environment variable set:

```sh
scripts/build-binaries.sh --ffmpeg-only
```

Then pass it to the build script with `-FFmpeg build\prefix-windows-x86_64`.

## Testing the tools

```sh
python3 scripts/test-binaries.py --require-ffmpeg --gpu bin/<platform>
```

runs every tool on a generated audio file and compares the MusicNN predictions with [`scripts/reference-predictions.json`](./scripts/reference-predictions.json), the results of the TensorFlow based `tools-2`. `--gpu` also runs the models on the GPU, falling back to the CPU without one, `--require-gpu` fails when the GPU isn't used.

## Models

The models in `models/` are Essentia's MusicNN models converted to ONNX. [`scripts/convert-models.py`](./scripts/convert-models.py) downloads the original TensorFlow graphs, converts them and checks that the predictions match, see the script for the Python packages it needs. Only needed when the models change.

## Releasing the analysis tools

The tools are versioned separately from the plugin since they rarely change. Publish a new tools release when `extractors/`, `models/` or the dependencies in the build scripts change:

1. Run the **Analysis tools** workflow from the Actions tab with the next release number, e.g. `5`. It builds and tests the tools on every platform, the GPU tools for Linux and Windows, then publishes the `tools-5` release with checksums and build provenance. Release numbers are plain integers, Picard would mistake tags like `tools-v5.0.0` for plugin versions.
2. Update the release number in the README and in `TOOLS_RELEASE` in `constants.py`, and copy the checksums from the release's `SHA256SUMS` to `TOOLS_CHECKSUMS` in `constants.py`: the archives ending in `-gpu` go to `"gpu"`, the others to `"cpu"`. The install button only accepts archives matching these checksums.

Pull requests that change the tools run the same builds without publishing anything.

## Releasing the plugin

1. Update `version` in `MANIFEST.toml` and the release tag in "Getting started" in the README. If the tools changed, publish and pin a new tools release first, see above.
2. Check the release with `scripts/check-release.py v<version>`, then commit, tag the commit with `v<version>`, e.g. `v3.3.0`, and push the tag.

Pushing the tag publishes a GitHub release for it after running the same check. Tags have to look like `v1.2.3`, Picard ignores other tags when updating plugins installed from a tag.
