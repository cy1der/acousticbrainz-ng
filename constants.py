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

BUILD_INSTRUCTIONS = "Build them with scripts/build-binaries.sh (Linux, macOS) or scripts/build-binaries.ps1 (Windows), see the README"

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
