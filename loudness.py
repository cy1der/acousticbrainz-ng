import json
import math
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .audio import AudioTools
from .cache import read_json, write_json
from .constants import ALBUM_LOUDNESS_FILE, TRACK_LOUDNESS_FILE
from .process import AnalysisCancelled
from .settings import Settings


@dataclass
class AlbumJob:
    files: List[str]
    # Identifies the album's track list, used to invalidate the cached album
    # loudness when tracks are added or removed
    key: List[str]
    track_count: int
    # Whether the album loudness may be measured, otherwise only a previously
    # cached measurement which included the track is used
    measure: bool


class Loudness:
    """ReplayGain and R128 gains measured with FFmpeg's loudnorm filter"""

    def __init__(self, audio: AudioTools):
        self.audio = audio
        self.runner = audio.runner
        self.log = audio.log
        self._album_locks: Dict[str, threading.Lock] = {}
        self._album_locks_lock = threading.Lock()

    def _album_lock(self, path: str) -> threading.Lock:
        with self._album_locks_lock:
            return self._album_locks.setdefault(path, threading.Lock())

    def measure(self, settings: Settings, args: List[str], description: str) -> Optional[Dict[str, float]]:
        # The loudnorm measurement of the input does not depend on the target
        # loudness, so a single pass covers both ReplayGain and R128
        ffmpeg_path = settings.ffmpeg_path
        if not ffmpeg_path:
            self.log.error("FFmpeg path not configured")
            return None

        proc = self.runner.run([ffmpeg_path, "-hide_banner", "-nostdin", *args, "-f", "null", "-"])

        if proc.returncode != 0:
            self.runner.log_failure(f"FFmpeg loudness measurement failed for {description}", proc)
            return None

        output = proc.stderr or ""
        start = output.rfind("{")
        end = output.rfind("}")
        if start == -1 or end < start:
            self.log.error(f"FFmpeg loudness measurement for {description} produced no JSON output")
            return None

        try:
            data = json.loads(output[start:end + 1])
        except json.JSONDecodeError as e:
            self.log.error(f"Failed to parse FFmpeg loudness output for {description}: {e}")
            return None

        measurement = {}
        for key in ("input_i", "input_tp", "input_lra"):
            try:
                value = float(data.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                measurement[key] = value
        return measurement

    def measure_album(self, settings: Settings, album_track_files: List[str]) -> Optional[Dict[str, float]]:
        # The concat filter converts differing sample rates and formats into a
        # single stream. The concat demuxer would restart the loudness filter
        # whenever the audio parameters change and only measure part of the album
        album_track_files = sorted(album_track_files)
        input_args = []
        for audio_file in album_track_files:
            input_args += ["-i", audio_file]

        # Automatic channel layout conversion between e.g. stereo and 5.1 skews
        # the measurement, so convert everything to stereo in that case
        channel_counts = {self.audio.get_channel_count(settings, audio_file) for audio_file in album_track_files}
        input_filter = "aformat=channel_layouts=stereo" if len(channel_counts) > 1 else "anull"
        if len(channel_counts) > 1:
            self.log.warning(f"Album files have different channel counts {sorted(channel_counts)}, measuring album loudness as stereo")

        filter_graph = "".join(f"[{i}:a:0]{input_filter}[a{i}];" for i in range(len(album_track_files)))
        filter_graph += "".join(f"[a{i}]" for i in range(len(album_track_files)))
        filter_graph += f"concat=n={len(album_track_files)}:v=0:a=1,loudnorm=print_format=json"

        return self.measure(
            settings,
            [*input_args, "-filter_complex", filter_graph],
            f"album with {len(album_track_files)} files"
        )

    def _album_measurement(self, settings: Settings, cache_folder: str, track_measurement: Dict[str, float], album: AlbumJob, entry: str) -> Optional[Dict[str, float]]:
        album_file = os.path.join(os.path.dirname(cache_folder), ALBUM_LOUDNESS_FILE)

        if not album.measure:
            album_data = read_json(album_file)
            if album_data is not None and entry in (album_data.get("key") or []):
                return album_data.get("measurement") or {}
            if album.track_count == 1:
                return track_measurement
            return None

        if len(album.files) == 1:
            return track_measurement

        # The album loudness is shared between all of its tracks, make sure
        # concurrent analyses of the same album only compute it once
        with self._album_lock(album_file):
            album_data = read_json(album_file)
            if album_data is not None and album_data.get("key") == album.key:
                return album_data.get("measurement") or {}

            album_measurement = self.measure_album(settings, album.files)
            if album_measurement is not None:
                write_json(album_file, {"key": album.key, "measurement": album_measurement})
            return album_measurement

    def apply(self, settings: Settings, file_path: str, cache_folder: str, album: Optional[AlbumJob], entry: str, tags: Dict[str, Any], measure: bool = True) -> bool:
        """Adds the loudness tags of a file, measuring what isn't cached yet
        unless measure is False"""
        try:
            track_file = os.path.join(cache_folder, TRACK_LOUDNESS_FILE)
            track_measurement = read_json(track_file)
            if track_measurement is None:
                if not measure:
                    return False
                track_measurement = self.measure(settings, ["-i", file_path, "-vn", "-sn", "-af", "loudnorm=print_format=json"], file_path)
                if track_measurement is None:
                    self.log.error("Failed to calculate track loudness")
                    return False
                write_json(track_file, track_measurement)

            is_opus = self.audio.is_opus(file_path)
            tags.update(self.tags(settings, track_measurement, "track", is_opus))
            tags["replaygain_reference_loudness"] = f"{settings.replaygain_reference_loudness:.2f} LUFS"

            if album is None:
                return True

            album_measurement = self._album_measurement(settings, cache_folder, track_measurement, album, entry)
            if album_measurement is None:
                if album.measure:
                    self.log.error("Failed to calculate album loudness")
                    return False
                # Nothing cached for the album yet, which isn't an error
                return True

            tags.update(self.tags(settings, album_measurement, "album", is_opus))
            return True
        except AnalysisCancelled:
            raise
        except Exception as e:
            self.log.error(f"Error calculating loudness: {e}")
            return False

    @staticmethod
    def tags(settings: Settings, measurement: Dict[str, float], scope: str, is_opus: bool) -> Dict[str, Any]:
        tags: Dict[str, Any] = {}
        input_i = measurement.get("input_i")
        input_tp = measurement.get("input_tp")
        input_lra = measurement.get("input_lra")

        if input_i is not None:
            tags[f"replaygain_{scope}_gain"] = f"{settings.replaygain_reference_loudness - input_i:.2f} dB"
        if input_tp is not None:
            tags[f"replaygain_{scope}_peak"] = f"{10 ** (input_tp / 20):.6f}"
        if input_lra is not None:
            tags[f"replaygain_{scope}_range"] = f"{input_lra:.2f} dB"
        if is_opus and input_i is not None:
            # R128 gains are relative to -23 LUFS, stored as Q7.8 fixed point
            tags[f"r128_{scope}_gain"] = max(-32768, min(32767, int(round((-23 - input_i) * 256))))

        return tags
