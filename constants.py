import os
from typing import Dict, List, Tuple, Union

PLUGIN_NAME = "AcousticBrainz-ng"

REQUIRED_MODELS: List[Tuple[str, str]] = [
    ("msd-musicnn-1", "msd"),
    ("mood_acoustic-musicnn-mtt-2", "mood_acoustic"),
    ("mood_aggressive-musicnn-mtt-2", "mood_aggressive"),
    ("mood_electronic-musicnn-msd-2", "mood_electronic"),
    ("mood_happy-musicnn-msd-2", "mood_happy"),
    ("mood_party-musicnn-mtt-2", "mood_party"),
    ("mood_relaxed-musicnn-msd-2", "mood_relaxed"),
    ("mood_sad-musicnn-msd-2", "mood_sad"),
    ("danceability-musicnn-msd-2", "danceability"),
    ("gender-musicnn-msd-2", "gender"),
    ("tonal_atonal-musicnn-mtt-2", "tonal_atonal"),
    ("voice_instrumental-musicnn-msd-2", "voice_instrumental")
]

OPTIONAL_MODELS: List[Tuple[str, str]] = [
    ("genre_electronic-musicnn-msd-2", "genre_electronic"),
    ("genre_rosamerica-musicnn-msd-2", "genre_rosamerica"),
    ("genre_tzanetakis-musicnn-msd-2", "genre_tzanetakis")
]

# Built from the sources in extractors/ by the scripts in scripts/
REQUIRED_BINARIES: List[str] = [
    "abng_key",
    "abng_musicnn",
    "abng_rhythm",
]

# The release of the tools (GitHub release tools-<number>) this version of the
# plugin needs, see CONTRIBUTING.md
TOOLS_RELEASE = 4

TOOLS_URL = "https://github.com/cy1der/acousticbrainz-ng/releases/download/tools-{release}/{name}"

# The tools come in two variants: "cpu", and "gpu" which also includes the
# WebGPU execution provider. On macOS the "cpu" tools already run the models
# on the GPU with CoreML, there are no "gpu" tools
TOOLS_VARIANTS = ("cpu", "gpu")

# SHA-256 of the archives of TOOLS_RELEASE by variant and platform, from the
# release's SHA256SUMS. Pinned here so that a download is only trusted if it
# matches what this version of the plugin expects
TOOLS_CHECKSUMS: Dict[str, Dict[str, str]] = {
    "cpu": {
        "linux-x86_64": "ec1e618380c9665950a08f459ad1cbd54f280c3b215505e393cab8e87a568ff8",
        "linux-arm64": "c41502984b7dc02db3f15dfa431cafb2002a670ea596e7a25217f40545003625",
        "macos-arm64": "bf40d69a5810960d7a6bc9c26bae168a8781451d63aae00a7f561a964bf0b007",
        "macos-x86_64": "84d353324af56dc1f7fc07f7c2f131dd90120bad3460832bd879a7568205e550",
        "windows-x86_64": "92723610889d20dde6e9a4fe1cd49b24048f2197731e03c4ac22f13fd2468a4b",
    },
    "gpu": {
        "linux-x86_64": "25f3ce7bd786b37a68f82c0671c5b58d25f6914aa6ff53b8f2c00831ce5ca6c8",
        "linux-arm64": "e5f3b69fb98030bbf12311ab007afee24fb0631710d7327cd5dfc7995905f0d4",
        "windows-x86_64": "1414de5ff463383b7a2759de88085c65a3b37aac473c19b6ed35a848401fc25b",
    },
}

# Platforms without their own build that can run another platform's tools
TOOLS_PLATFORM_ALIASES: Dict[str, str] = {
    "windows-arm64": "windows-x86_64",
}

# Which GPU the tools use when there are several, see abng_musicnn's
# --gpu-preference: "high-performance" prefers a discrete one, "low-power" an
# integrated one
GPU_PREFERENCES = ("high-performance", "low-power")
DEFAULT_GPU_PREFERENCE = "high-performance"

# Name of the WebGPU execution provider library in the "gpu" tools
WEBGPU_LIBRARIES = ("libonnxruntime_providers_webgpu.so", "onnxruntime_providers_webgpu.dll")

BUILD_INSTRUCTIONS = (
    "Install them with the \"Install tools\" button in the plugin options, or build them with "
    "scripts/build-binaries.sh (Linux, macOS) or scripts/build-binaries.ps1 (Windows), see the README"
)

FFMPEG_MISSING = "FFmpeg not found, install the analysis tools in the plugin options, they include it, or set its path there"

# Number of MusicNN patches per model run. Doesn't change the results. On the
# CPU larger batches are slower and need more memory, on the GPU 16 is the
# fastest
MUSICNN_CPU_BATCH_SIZE = 4
MUSICNN_GPU_BATCH_SIZE = 16

# Total number of MusicNN class probabilities across all models, i.e. the
# number of values encoded into the fingerprint image
FINGERPRINT_VALUES = 95
FINGERPRINT_COMMENT = f"{PLUGIN_NAME} fingerprint"

TRACK_LOUDNESS_FILE = "loudness.json"
ALBUM_LOUDNESS_FILE = "album_loudness.json"

MAX_MUSICNN_WORKERS = max(len(REQUIRED_MODELS), len(OPTIONAL_MODELS))
MAX_CONCURRENT_ANALYSES = 8

ENV = os.environ.copy()

# Plugin options as (name, default, bounds). Empty paths mean "use the
# bundled/auto-detected location", resolved at runtime so that the plugin
# keeps working when its install directory changes
PLUGIN_OPTIONS: List[Tuple[str, Union[str, int, bool], Union[Tuple[int, int], None]]] = [
    ("binaries_path", "", None),
    ("ffmpeg_path", "", None),
    ("models_path", "", None),
    ("cache_path", "", None),
    ("max_musicnn_workers", 4, (1, MAX_MUSICNN_WORKERS)),
    ("max_concurrent_analyses", 2, (1, MAX_CONCURRENT_ANALYSES)),
    ("replaygain_reference_loudness", -18, (-30, -5)),
    ("analyze_optional", False, None),
    ("save_raw", False, None),
    ("calculate_replaygain", True, None),
    ("save_fingerprint", False, None),
    ("auto_apply_cached", True, None),
    ("use_gpu", False, None),
    ("gpu_preference", DEFAULT_GPU_PREFERENCE, None),
]

# Option names used by the Picard 2 version of this plugin, stored in the
# global "setting" section, mapped to the new plugin-private option names
LEGACY_OPTION_PREFIX = "acousticbrainz_ng_"
LEGACY_OPTION_TYPES: Dict[str, type] = {
    "ffmpeg_path": str,
    "max_musicnn_workers": int,
    "max_concurrent_analyses": int,
    "replaygain_reference_loudness": int,
    "analyze_optional": bool,
    "save_raw": bool,
    "calculate_replaygain": bool,
    "save_fingerprint": bool,
}
