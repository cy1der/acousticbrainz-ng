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
# plugin needs, see "Analysis tools" in the README
TOOLS_RELEASE = 1

TOOLS_URL = "https://github.com/cy1der/acousticbrainz-ng/releases/download/tools-{release}/{name}"

# SHA-256 of the archives of TOOLS_RELEASE by platform, from the release's
# SHA256SUMS. Pinned here so that a download is only trusted if it matches
# what this version of the plugin expects
TOOLS_CHECKSUMS: Dict[str, str] = {
    "linux-x86_64": "1e5c6437b7a6522536b54bc3035e2f1dc49ea8b59a8aa301d5186676bf5d11c6",
    "linux-arm64": "10a23fb1f0df0f233c37ec92f8c38fc1ee33ac4d0a4ad500cb49b085bd3f0743",
    "macos-arm64": "c953834a5d22d30c1303c8687c49443e627483a06de4beeb6578236dcff55b0b",
    "macos-x86_64": "d434ffad7d6f610762853313c6a7c88ffaf45214133070411165f9510c54c31c",
    "windows-x86_64": "09b16158cf62660035526103e66dc8768497da4515c554da1e239f177a21ad89",
}

# Platforms without their own build that can run another platform's tools
TOOLS_PLATFORM_ALIASES: Dict[str, str] = {
    "windows-arm64": "windows-x86_64",
}

BUILD_INSTRUCTIONS = (
    "Install them with the \"Install tools\" button in the plugin options, or build them with "
    "scripts/build-binaries.sh (Linux, macOS) or scripts/build-binaries.ps1 (Windows), see the README"
)

FFMPEG_MISSING = "FFmpeg not found, install the analysis tools in the plugin options, they include it, or set its path there"

# Number of MusicNN patches per TensorFlow run. Doesn't change the results,
# but the default of 64 needs about 4 times as much memory
MUSICNN_BATCH_SIZE = 4

# Total number of MusicNN class probabilities across all models, i.e. the
# number of values encoded into the fingerprint image
FINGERPRINT_VALUES = 95
FINGERPRINT_COMMENT = f"{PLUGIN_NAME} fingerprint"

TRACK_LOUDNESS_FILE = "loudness.json"
ALBUM_LOUDNESS_FILE = "album_loudness.json"

MAX_MUSICNN_WORKERS = max(len(REQUIRED_MODELS), len(OPTIONAL_MODELS))
MAX_CONCURRENT_ANALYSES = 8

ENV = os.environ.copy()
ENV['TF_ENABLE_ONEDNN_OPTS'] = "0"

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
