import os
import sys
from typing import List, Tuple
from picard.config import BoolOption, TextOption, IntOption

PLUGIN_NAME = "AcousticBrainz-ng"
PLUGIN_AUTHOR = "cy1der"
PLUGIN_DESCRIPTION = """
Analyze track acoustic characteristics using Essentia
<br/>
This plugin is not affiliated with the <a href='https://acousticbrainz.org'>AcousticBrainz</a> project<br/>
This is not a 1:1 recreation of the AcousticBrainz schema, but will provide most of the meaningful data<br/>
External dependencies:
<ul>
<li><a href='https://essentia.upf.edu'>Essentia</a> binaries compiled with TensorFlow and gaia2 support</li>
<li>A few MusicNN models (see user guide for details)</li>
<li><a href='https://ffmpeg.org'>FFmpeg</a></li>
</ul>
<strong>This plugin is CPU heavy!</strong>
"""
PLUGIN_VERSION = "1.0.0"
PLUGIN_API_VERSIONS = ["2.7", "2.8", "2.9", "2.10", "2.11"]
PLUGIN_LICENSE = "GPL-2.0-or-later"
PLUGIN_LICENSE_URL = "https://www.gnu.org/licenses/gpl-2.0.html"
PLUGIN_USER_GUIDE_URL = "https://example.com" # TODO: Update with actual user guide URL

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

REQUIRED_BINARIES: List[str] = [
    "streaming_extractor_music",
    "streaming_musicnn_predict",
    "streaming_md5",
]

ENV = os.environ.copy()
ENV['TF_ENABLE_ONEDNN_OPTS'] = "0"

CONFIG_OPTIONS = [
    TextOption("setting", "acousticbrainz_ng_binaries_path", os.path.join(os.path.dirname(__file__), "bin")),
    TextOption("setting", "acousticbrainz_ng_ffmpeg_path", os.path.join(os.path.dirname(sys.executable), "ffmpeg" + (".exe" if os.name == "nt" else ""))),
    TextOption("setting", "acousticbrainz_ng_models_path", os.path.join(os.path.dirname(__file__), "models")),
    TextOption("setting", "acousticbrainz_ng_cache_path", os.path.join(os.path.dirname(__file__), "cache")),
    IntOption("setting", "acousticbrainz_ng_max_musicnn_workers", 4),
    IntOption("setting", "acousticbrainz_ng_replaygain_reference_loudness", -18),
    BoolOption("setting", "acousticbrainz_ng_analyze_optional", False),
    BoolOption("setting", "acousticbrainz_ng_save_raw", False),
    BoolOption("setting", "acousticbrainz_ng_calculate_replaygain", True)
]

GAIA_KEY_ALGORITHMS = ["edma", "krumhansl", "temperley"]