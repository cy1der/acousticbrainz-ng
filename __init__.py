import os
import re
import json
import shutil
import subprocess
import hashlib
import zlib
import struct
import threading
import concurrent.futures
import tempfile
import math
import yaml
from dataclasses import dataclass, field
from functools import partial
from typing import Any, List, Tuple, Dict, Optional
from picard.plugin3.api import (
    Album,
    BaseAction,
    CoverArtImage,
    OptionsPage,
    PluginApi,
    Track,
)
from picard.util import thread
from PyQt6 import QtWidgets, QtCore

from .constants import *

try:
    from picard.const.appdirs import cache_folder as _picard_cache_folder
except ImportError:
    def _picard_cache_folder() -> str:
        return QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.CacheLocation)

FINGERPRINT_COMMENT = f"{PLUGIN_NAME} fingerprint"
TRACK_LOUDNESS_FILE = "loudness.json"
ALBUM_LOUDNESS_FILE = "album_loudness.json"


def _exe(name: str) -> str:
    return name + (".exe" if os.name == 'nt' else "")


def _default_path(api: PluginApi, name: str) -> str:
    if name == "binaries_path":
        return str(api.plugin_dir / "bin")
    if name == "models_path":
        return str(api.plugin_dir / "models")
    if name == "cache_path":
        return os.path.join(_picard_cache_folder(), "acousticbrainz-ng")
    if name == "ffmpeg_path":
        return shutil.which("ffmpeg") or ""
    raise KeyError(name)


def _resolve_path(api: PluginApi, name: str, value: Optional[str] = None) -> str:
    if value is None:
        value = api.plugin_config[name]
    return value or _default_path(api, name)


class AnalysisCancelled(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    binaries_path: str
    ffmpeg_path: str
    models_path: str
    cache_path: str
    max_musicnn_workers: int
    max_concurrent_analyses: int
    replaygain_reference_loudness: int
    analyze_optional: bool
    save_raw: bool
    calculate_replaygain: bool
    save_fingerprint: bool


@dataclass
class AlbumJob:
    files: List[str]
    # Identifies the album's track list, used to invalidate the cached album
    # loudness when tracks are added or removed
    key: List[str]


@dataclass
class FileJob:
    # Picard File object, only accessed on the main thread
    file: Any
    filename: str
    metadata: Dict[str, str]


@dataclass
class FileResult:
    file: Any
    tags: Dict[str, Any] = field(default_factory=dict)
    fingerprint: Optional[Tuple[bytes, str]] = None
    errors: List[str] = field(default_factory=list)
    processed: bool = False


@dataclass
class TrackResult:
    files: List[FileResult]
    cancelled: bool = False


class ProcessingFile:
    """Lazily provides a stereo version of an audio file for Essentia, only
    probing and downmixing when an analysis actually needs to run"""

    def __init__(self, plugin: "AcousticBrainzNG", settings: Settings, file_path: str):
        self._plugin = plugin
        self._settings = settings
        self._file_path = file_path
        self._processing_file: Optional[str] = None
        self._temp_file: Optional[str] = None

    def get(self) -> str:
        if self._processing_file is None:
            self._processing_file, self._temp_file = self._plugin._get_processing_file(self._settings, self._file_path)
        return self._processing_file

    def __enter__(self) -> "ProcessingFile":
        return self

    def __exit__(self, *exc) -> None:
        if self._temp_file:
            self._plugin._cleanup_temp_file(self._temp_file)


class AcousticBrainzNG:
    def __init__(self, api: PluginApi):
        self.api = api
        self.log = api.logger
        self.pool = QtCore.QThreadPool()
        self.in_flight: set = set()
        self._cancelled = threading.Event()
        self._processes: set = set()
        self._processes_lock = threading.Lock()
        self._album_locks: Dict[str, threading.Lock] = {}
        self._album_locks_lock = threading.Lock()

    def settings(self) -> Settings:
        cfg = self.api.plugin_config
        return Settings(
            binaries_path=_resolve_path(self.api, "binaries_path"),
            ffmpeg_path=_resolve_path(self.api, "ffmpeg_path"),
            models_path=_resolve_path(self.api, "models_path"),
            cache_path=_resolve_path(self.api, "cache_path"),
            max_musicnn_workers=cfg["max_musicnn_workers"],
            max_concurrent_analyses=cfg["max_concurrent_analyses"],
            replaygain_reference_loudness=cfg["replaygain_reference_loudness"],
            analyze_optional=cfg["analyze_optional"],
            save_raw=cfg["save_raw"],
            calculate_replaygain=cfg["calculate_replaygain"],
            save_fingerprint=cfg["analyze_optional"] and cfg["save_fingerprint"],
        )

    def shutdown(self) -> None:
        self._cancelled.set()
        self.pool.clear()
        with self._processes_lock:
            for proc in self._processes:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.pool.waitForDone()

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise AnalysisCancelled()

    def _run(self, args: List[str]) -> subprocess.CompletedProcess:
        self._check_cancelled()
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=ENV,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        with self._processes_lock:
            self._processes.add(proc)
        try:
            stdout, stderr = proc.communicate()
        finally:
            with self._processes_lock:
                self._processes.discard(proc)
        self._check_cancelled()
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)

    def _log_process_failure(self, description: str, proc: subprocess.CompletedProcess) -> None:
        self.log.error(f"{description} with exit code {proc.returncode}")
        if proc.stdout:
            self.log.error(f"stdout: {proc.stdout}")
        if proc.stderr:
            self.log.error(f"stderr: {proc.stderr}")

    @staticmethod
    def _get_binary_path(binary_name: str, binaries_path: str) -> str:
        return os.path.join(binaries_path, _exe(binary_name))

    def _get_binary_paths(self, settings: Settings) -> Tuple[str, str, str]:
        binaries_path = settings.binaries_path
        if not binaries_path:
            raise ValueError("Binaries path not configured")

        musicnn_binary_path = self._get_binary_path("streaming_musicnn_predict", binaries_path)
        rhythm_binary_path = self._get_binary_path("streaming_rhythmextractor_multifeature", binaries_path)
        key_binary_path = self._get_binary_path("streaming_key", binaries_path)

        if not os.path.exists(musicnn_binary_path):
            raise FileNotFoundError(f"Binary {musicnn_binary_path} not found")
        if not os.path.exists(rhythm_binary_path):
            raise FileNotFoundError(f"Binary {rhythm_binary_path} not found")
        if not os.path.exists(key_binary_path):
            raise FileNotFoundError(f"Binary {key_binary_path} not found")

        return musicnn_binary_path, rhythm_binary_path, key_binary_path

    @staticmethod
    def _pending_models(models: List[Tuple[str, str]], output_path: str) -> List[Tuple[str, str]]:
        return [model for model in models if not os.path.exists(os.path.join(output_path, f"{model[1]}.json"))]

    def _run_musicnn_models(self, settings: Settings, models: List[Tuple[str, str]], musicnn_binary_path: str, file: str, output_path: str) -> bool:
        models_path = settings.models_path
        if not models_path:
            self.log.error("Models path not configured")
            return False

        success_results = {}
        def run_musicnn_model(model_info):
            model_name, output_file = model_info
            try:
                model_path = os.path.join(models_path, f"{model_name}.pb")

                if not os.path.exists(model_path):
                    raise FileNotFoundError(f"Model {model_name} not found at {model_path}")

                output_file_path = os.path.join(output_path, f"{output_file}.json")

                if os.path.exists(output_file_path):
                    success_results[model_name] = True
                    return

                result = self._run([musicnn_binary_path, model_path, file, output_file_path])

                if result.returncode != 0:
                    success_results[model_name] = False
                    self._log_process_failure(f"MusicNN binary {musicnn_binary_path} failed for model {model_name} on file {file}", result)
                else:
                    success_results[model_name] = True
            except AnalysisCancelled:
                success_results[model_name] = False
            except FileNotFoundError as e:
                success_results[model_name] = False
                self.log.error(f"Model {model_name} not found: {e}")
            except Exception as e:
                success_results[model_name] = False
                self.log.error(f"Error processing model {model_name}: {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=settings.max_musicnn_workers) as executor:
            futures = [executor.submit(run_musicnn_model, model) for model in models]
            concurrent.futures.wait(futures)
        self._check_cancelled()

        return all(success_results.get(model[0], False) for model in models)

    def analyze_required(self, settings: Settings, source: ProcessingFile, output_path: str) -> bool:
        if not self._check_binaries(settings):
            self.log.error("Essentia binaries not found")
            return False

        if not self._check_required_models(settings):
            self.log.error("Required models not found")
            return False

        try:
            musicnn_binary_path, rhythm_binary_path, key_binary_path = self._get_binary_paths(settings)
        except (ValueError, FileNotFoundError) as e:
            self.log.error(str(e))
            return False

        rhythm_yaml_path = os.path.join(output_path, "rhythm.yaml")
        key_yaml_path = os.path.join(output_path, "key.yaml")
        pending_models = self._pending_models(REQUIRED_MODELS, output_path)
        need_rhythm = not os.path.exists(rhythm_yaml_path)
        need_key = not os.path.exists(key_yaml_path)

        if not (pending_models or need_rhythm or need_key):
            return True

        processing_file = source.get()

        rhythm_success = True
        def run_rhythm():
            nonlocal rhythm_success
            if not need_rhythm:
                return

            try:
                rhythm_proc = self._run([rhythm_binary_path, processing_file])
            except AnalysisCancelled:
                rhythm_success = False
                return

            if rhythm_proc.returncode != 0:
                rhythm_success = False
                self._log_process_failure(f"Rhythm binary {rhythm_binary_path} failed on file {processing_file}", rhythm_proc)
                return

            try:
                stdout = rhythm_proc.stdout or ""
                lines = stdout.splitlines(keepends=True)
                if not lines:
                    raise ValueError("Rhythm binary produced no stdout")

                yaml_lines = lines[-5:] if len(lines) >= 5 else lines
                yaml_str = "".join(yaml_lines)
                if not yaml_str.strip():
                    raise ValueError("Empty YAML section extracted from rhythm binary output")

                with open(rhythm_yaml_path, "w", encoding="utf-8") as f:
                    f.write(yaml_str)
            except Exception as e:
                rhythm_success = False
                self.log.error(f"Failed to extract/save rhythm.yaml from rhythm binary stdout: {e}")
                if rhythm_proc.stdout:
                    self.log.error(f"Rhythm stdout: {rhythm_proc.stdout}")
                if rhythm_proc.stderr:
                    self.log.error(f"Rhythm stderr: {rhythm_proc.stderr}")
                return

        key_success = True
        def run_key():
            nonlocal key_success
            if not need_key:
                return

            try:
                key_proc = self._run([key_binary_path, processing_file, key_yaml_path])
            except AnalysisCancelled:
                key_success = False
                return

            if key_proc.returncode != 0:
                key_success = False
                self._log_process_failure(f"Key binary {key_binary_path} failed on file {processing_file}", key_proc)
                return

        rhythm_thread = threading.Thread(target=run_rhythm)
        rhythm_thread.start()

        key_thread = threading.Thread(target=run_key)
        key_thread.start()

        musicnn_success = True
        try:
            if pending_models:
                musicnn_success = self._run_musicnn_models(settings, pending_models, musicnn_binary_path, processing_file, output_path)
        finally:
            rhythm_thread.join()
            key_thread.join()
        self._check_cancelled()

        return rhythm_success and key_success and musicnn_success

    def analyze_optional(self, settings: Settings, source: ProcessingFile, output_path: str) -> bool:
        if not self._check_binaries(settings):
            self.log.error("Essentia binaries not found")
            return False

        if not self._check_optional_models(settings):
            self.log.error("Optional models not found")
            return False

        try:
            musicnn_binary_path = self._get_binary_paths(settings)[0]
        except (ValueError, FileNotFoundError) as e:
            self.log.error(str(e))
            return False

        pending_models = self._pending_models(OPTIONAL_MODELS, output_path)
        if not pending_models:
            return True

        return self._run_musicnn_models(settings, pending_models, musicnn_binary_path, source.get(), output_path)

    def _load_model_output(self, settings: Settings, model: str, output: str, output_path: str) -> Optional[Tuple[List[str], List[float]]]:
        model_json_path = os.path.join(settings.models_path, f"{model}.json")
        if not os.path.exists(model_json_path):
            self.log.error(f"Model JSON metadata not found: {model_json_path}")
            return None

        output_file_path = os.path.join(output_path, f"{output}.json")
        if not os.path.exists(output_file_path):
            self.log.error(f"Output file not found: {output_file_path}")
            return None

        try:
            with open(model_json_path, 'r', encoding='utf-8') as f:
                model_metadata = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self.log.error(f"Error reading model metadata file: {e}")
            return None

        try:
            with open(output_file_path, 'r', encoding='utf-8') as f:
                output_data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            # Most likely left behind by an interrupted run, remove it so that
            # the next analysis recomputes it instead of failing forever
            self.log.error(f"Error reading output file, removing it: {e}")
            try:
                os.remove(output_file_path)
            except OSError:
                pass
            return None

        values = (output_data.get("predictions") or {}).get("mean")
        if not values:
            self.log.error(f"No predictions found in output data for {model}")
            return None

        classes = model_metadata.get("classes")
        if not classes or len(classes) != len(values):
            self.log.error(f"No or invalid classes defined in model metadata for {model}")
            return None

        return classes, values

    @staticmethod
    def _raw_tags(output: str, classes: List[str], values: List[float]) -> Dict[str, float]:
        return {
            f"ab:hi:{output}:{class_name.replace('non', 'not').replace('_', ' ').lower()}": value
            for class_name, value in zip(classes, values)
        }

    def parse_required(self, settings: Settings, output_path: str, tags: Dict[str, Any]) -> bool:
        if not self._check_required_models(settings):
            self.log.error("Required models not found")
            return False

        moods = []
        msd_tags = []

        for model, output in REQUIRED_MODELS:
            loaded = self._load_model_output(settings, model, output, output_path)
            if loaded is None:
                return False
            classes, values = loaded

            if len(classes) == 2:
                max_index = values.index(max(values))
                moods.append(self._format_class(classes[max_index]))
            elif model == REQUIRED_MODELS[0][0]:
                top5 = sorted(zip(classes, values), key=lambda x: x[1], reverse=True)[:5]

                for class_name, _ in top5:
                    msd_tags.append(class_name[0].upper() + class_name[1:] if class_name else "")

            if settings.save_raw:
                tags.update(self._raw_tags(output, classes, values))

        tags['mood'] = moods
        tags['tags'] = msd_tags

        rhythm_yaml_path = os.path.join(output_path, "rhythm.yaml")
        key_yaml_path = os.path.join(output_path, "key.yaml")

        try:
            rhythm_data = self._load_yaml(rhythm_yaml_path, "rhythm")
            key_data = self._load_yaml(key_yaml_path, "key")
            if rhythm_data is None or key_data is None:
                return False

            tags["bpm"] = int(round(rhythm_data["bpm"]))
            tags["key"] = "o" if key_data["tonal"]["key_scale"] == "off" else f"{key_data['tonal']['key']}{'m' if key_data['tonal']['key_scale'] == 'minor' else ''}"

            if settings.save_raw:
                tags["ab:lo:tonal:key_scale"] = key_data["tonal"]["key_scale"]
                tags["ab:lo:tonal:key_key"] = key_data["tonal"]["key"]

            return True
        except Exception as e:
            self.log.error(f"Error processing feature data: {e}")
            return False

    def _load_yaml(self, path: str, name: str) -> Optional[Dict]:
        if not os.path.exists(path):
            self.log.error(f"{name.capitalize()} YAML file not found: {path}")
            return None

        with open(path, 'r', encoding='utf-8') as f:
            loaded = yaml.safe_load(f)

        if not isinstance(loaded, dict):
            self.log.error(f"Invalid {name} YAML format: expected a mapping at the top level")
            return None

        return loaded

    def parse_optional(self, settings: Settings, output_path: str, tags: Dict[str, Any]) -> bool:
        if not self._check_optional_models(settings):
            self.log.error("Optional models not found")
            return False

        for model, output in OPTIONAL_MODELS:
            loaded = self._load_model_output(settings, model, output, output_path)
            if loaded is None:
                return False

            if settings.save_raw:
                tags.update(self._raw_tags(output, *loaded))

        return True

    def build_fingerprint(self, settings: Settings, output_path: str) -> Optional[Tuple[bytes, str]]:
        # Values are read from the cached model outputs in a fixed order, so the
        # fingerprint doesn't depend on "Save raw values" or on the tag order
        # of previously saved files
        fingerprint_data: List[float] = []

        for model, output in REQUIRED_MODELS + OPTIONAL_MODELS:
            loaded = self._load_model_output(settings, model, output, output_path)
            if loaded is None:
                return None
            fingerprint_data.extend(float(value) for value in loaded[1])

        if len(fingerprint_data) != FINGERPRINT_VALUES:
            self.log.error(f"Fingerprint expected exactly {FINGERPRINT_VALUES} values, got {len(fingerprint_data)}")
            return None

        fingerprint_file = os.path.join(output_path, "fingerprint.png")

        try:
            png = _fingerprint_png(fingerprint_data)
            with open(fingerprint_file, "wb") as f:
                f.write(png)
        except Exception as e:
            self.log.error(f"Failed to create fingerprint PNG: {e}")
            return None

        return png, fingerprint_file

    @staticmethod
    def _format_class(class_name: str) -> str:
        return class_name.replace("non", "not").replace("_", " ").capitalize()

    def _generate_cache_folder(self, settings: Settings, metadata: Dict[str, str], file_path: str, create: bool = True) -> str:
        cache_base = settings.cache_path
        if not cache_base:
            raise ValueError("Cache path not configured")

        release_artist_mbid = metadata.get('musicbrainz_albumartistid') or 'NO_MBID'
        release_group_mbid = metadata.get('musicbrainz_releasegroupid') or 'NO_MBID'
        release_mbid = metadata.get('musicbrainz_albumid') or 'NO_MBID'
        recording_mbid = metadata.get('musicbrainz_recordingid')

        if not recording_mbid:
            recording_mbid = self._get_audio_hash(settings, file_path)

        cache_folder = os.path.join(
            str(cache_base),
            str(release_artist_mbid) if len(str(release_artist_mbid).encode('utf-8')) <= 255 else hashlib.sha256(str(release_artist_mbid).encode('utf-8')).hexdigest(),
            str(release_group_mbid),
            str(release_mbid),
            str(recording_mbid)
        )

        if create:
            os.makedirs(cache_folder, exist_ok=True)

        return cache_folder

    def _get_audio_hash(self, settings: Settings, file_path: str) -> str:
        try:
            binaries_path = settings.binaries_path
            if not binaries_path:
                raise ValueError("Binaries path not configured")

            binary_path = self._get_binary_path("streaming_md5", binaries_path)

            result = self._run([binary_path, file_path])

            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line.startswith('MD5:'):
                        return line.split('MD5:')[1].strip()
            else:
                self._log_process_failure(f"MD5 binary {binary_path} failed on file {file_path}", result)

            self.log.error(f"Failed to calculate audio hash for file {file_path}: MD5 not found in output")

        except AnalysisCancelled:
            raise
        except Exception as e:
            self.log.error(f"Error calculating audio hash: {e}")

        return f"fallback_{hashlib.md5(file_path.encode('utf-8')).hexdigest()}"

    def _check_binaries(self, settings: Settings) -> bool:
        path = settings.binaries_path

        if not path or not os.path.exists(path):
            return False

        for binary in REQUIRED_BINARIES:
            binary_path = self._get_binary_path(binary, path)
            if not os.path.exists(binary_path):
                return False

        return True

    @staticmethod
    def _check_models(settings: Settings, models: List[Tuple[str, str]]) -> bool:
        path = settings.models_path

        if not path or not os.path.exists(path):
            return False

        for model in models:
            model_path = os.path.join(path, f"{model[0]}.pb")
            if not os.path.exists(model_path):
                return False

        return True

    def _check_required_models(self, settings: Settings) -> bool:
        return self._check_models(settings, REQUIRED_MODELS)

    def _check_optional_models(self, settings: Settings) -> bool:
        return self._check_models(settings, OPTIONAL_MODELS)

    @staticmethod
    def _is_opus_file(file_path: str) -> bool:
        return file_path.lower().endswith('.opus')

    def _get_audio_channel_count(self, settings: Settings, file_path: str) -> int:
        try:
            ffmpeg_path = settings.ffmpeg_path
            if not ffmpeg_path:
                self.log.warning("FFmpeg path not configured, assuming stereo")
                return 2

            ffprobe_path = os.path.join(os.path.dirname(ffmpeg_path), _exe("ffprobe"))

            if not os.path.exists(ffprobe_path):
                probe_proc = self._run([ffmpeg_path, "-hide_banner", "-nostdin", "-i", file_path])

                output = probe_proc.stderr or ""

                channel_match = re.search(r'(\d+)\s+channels?', output, re.IGNORECASE)
                if channel_match:
                    return int(channel_match.group(1))

                if re.search(r'\b7\.1\b', output):
                    return 8
                elif re.search(r'\b5\.1\b', output):
                    return 6
                elif re.search(r'\bquad\b', output, re.IGNORECASE):
                    return 4
                elif re.search(r'\bstereo\b', output, re.IGNORECASE):
                    return 2
                elif re.search(r'\bmono\b', output, re.IGNORECASE):
                    return 1

                return 2
            else:
                probe_proc = self._run(
                    [ffprobe_path, "-v", "error", "-select_streams", "a:0",
                     "-show_entries", "stream=channels", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
                )

                if probe_proc.returncode == 0 and probe_proc.stdout.strip():
                    try:
                        return int(probe_proc.stdout.strip())
                    except ValueError:
                        pass

                return 2

        except AnalysisCancelled:
            raise
        except Exception as e:
            self.log.warning(f"Error detecting audio channels for {file_path}: {e}, assuming stereo")
            return 2

    def _create_stereo_downmix(self, settings: Settings, file_path: str) -> Optional[str]:
        ffmpeg_path = settings.ffmpeg_path
        if not ffmpeg_path:
            self.log.error("FFmpeg path not configured, cannot downmix")
            return None

        file_ext = os.path.splitext(file_path)[1]
        temp_dir = tempfile.mkdtemp(prefix="acousticbrainz_ng_")
        temp_file = os.path.join(temp_dir, f"stereo_downmix{file_ext}")
        success = False

        try:
            self.log.debug(f"Downmixing multichannel audio to stereo: {file_path} -> {temp_file}")

            downmix_proc = self._run([ffmpeg_path, "-hide_banner", "-nostdin", "-i", file_path, "-ac", "2", "-y", temp_file])

            if downmix_proc.returncode != 0:
                self._log_process_failure(f"FFmpeg downmix failed for {file_path}", downmix_proc)
            elif not os.path.exists(temp_file):
                self.log.error(f"Stereo downmix file was not created: {temp_file}")
            else:
                self.log.debug(f"Successfully created stereo downmix: {temp_file}")
                success = True
        except AnalysisCancelled:
            raise
        except Exception as e:
            self.log.error(f"Error creating stereo downmix for {file_path}: {e}")
        finally:
            if not success:
                shutil.rmtree(temp_dir, ignore_errors=True)

        return temp_file if success else None

    def _cleanup_temp_file(self, temp_file_path: str) -> None:
        try:
            if temp_file_path and os.path.exists(temp_file_path):
                temp_dir = os.path.dirname(temp_file_path)
                shutil.rmtree(temp_dir)
                self.log.debug(f"Cleaned up temporary directory: {temp_dir}")
        except Exception as e:
            self.log.warning(f"Failed to clean up temporary file {temp_file_path}: {e}")

    def _get_processing_file(self, settings: Settings, file_path: str) -> Tuple[str, Optional[str]]:
        channels = self._get_audio_channel_count(settings, file_path)

        if channels > 2:
            self.log.info(f"File {file_path} has {channels} channels, creating stereo downmix for processing")
            temp_file = self._create_stereo_downmix(settings, file_path)
            if temp_file:
                return (temp_file, temp_file)
            else:
                self.log.warning(f"Failed to create stereo downmix for {file_path}, attempting to process original")
                return (file_path, None)

        return (file_path, None)

    def _measure_loudness(self, settings: Settings, args: List[str], description: str) -> Optional[Dict[str, float]]:
        # The loudnorm measurement of the input does not depend on the target
        # loudness, so a single pass covers both ReplayGain and R128
        ffmpeg_path = settings.ffmpeg_path
        if not ffmpeg_path:
            self.log.error("FFmpeg path not configured")
            return None

        proc = self._run([ffmpeg_path, "-hide_banner", "-nostdin", *args, "-f", "null", "-"])

        if proc.returncode != 0:
            self._log_process_failure(f"FFmpeg loudness measurement failed for {description}", proc)
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

    def _measure_album_loudness(self, settings: Settings, album_track_files: List[str]) -> Optional[Dict[str, float]]:
        # The concat filter converts differing sample rates and formats into a
        # single stream. The concat demuxer would restart the loudness filter
        # whenever the audio parameters change and only measure part of the album
        album_track_files = sorted(album_track_files)
        input_args = []
        for audio_file in album_track_files:
            input_args += ["-i", audio_file]

        # Automatic channel layout conversion between e.g. stereo and 5.1 skews
        # the measurement, so convert everything to stereo in that case
        channel_counts = {self._get_audio_channel_count(settings, audio_file) for audio_file in album_track_files}
        input_filter = "aformat=channel_layouts=stereo" if len(channel_counts) > 1 else "anull"
        if len(channel_counts) > 1:
            self.log.warning(f"Album files have different channel counts {sorted(channel_counts)}, measuring album loudness as stereo")

        filter_graph = "".join(f"[{i}:a:0]{input_filter}[a{i}];" for i in range(len(album_track_files)))
        filter_graph += "".join(f"[a{i}]" for i in range(len(album_track_files)))
        filter_graph += f"concat=n={len(album_track_files)}:v=0:a=1,loudnorm=print_format=json"

        return self._measure_loudness(
            settings,
            [*input_args, "-filter_complex", filter_graph],
            f"album with {len(album_track_files)} files"
        )

    @staticmethod
    def _read_json(path: str) -> Optional[Dict]:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_json(path: str, data: Dict) -> None:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    def _album_lock(self, path: str) -> threading.Lock:
        with self._album_locks_lock:
            return self._album_locks.setdefault(path, threading.Lock())

    def calculate_loudness(self, settings: Settings, file_path: str, cache_folder: str, album: Optional[AlbumJob], tags: Dict[str, Any]) -> bool:
        try:
            track_file = os.path.join(cache_folder, TRACK_LOUDNESS_FILE)
            track_measurement = self._read_json(track_file)
            if track_measurement is None:
                track_measurement = self._measure_loudness(settings, ["-i", file_path, "-vn", "-sn", "-af", "loudnorm=print_format=json"], file_path)
                if track_measurement is None:
                    self.log.error("Failed to calculate track loudness")
                    return False
                self._write_json(track_file, track_measurement)

            is_opus = self._is_opus_file(file_path)
            tags.update(self._loudness_tags(settings, track_measurement, "track", is_opus))
            tags["replaygain_reference_loudness"] = f"{settings.replaygain_reference_loudness:.2f} LUFS"

            if album is None:
                return True

            if len(album.files) == 1:
                album_measurement = track_measurement
            else:
                # The album loudness is shared between all of its tracks, make
                # sure concurrent analyses of the same album only compute it once
                album_file = os.path.join(os.path.dirname(cache_folder), ALBUM_LOUDNESS_FILE)
                with self._album_lock(album_file):
                    album_data = self._read_json(album_file)
                    if album_data is not None and album_data.get("key") == album.key:
                        album_measurement = album_data.get("measurement") or {}
                    else:
                        album_measurement = self._measure_album_loudness(settings, album.files)
                        if album_measurement is None:
                            self.log.error("Failed to calculate album loudness")
                            return False
                        self._write_json(album_file, {"key": album.key, "measurement": album_measurement})

            tags.update(self._loudness_tags(settings, album_measurement, "album", is_opus))
            return True
        except AnalysisCancelled:
            raise
        except Exception as e:
            self.log.error(f"Error calculating loudness: {e}")
            return False

    @staticmethod
    def _loudness_tags(settings: Settings, measurement: Dict[str, float], scope: str, is_opus: bool) -> Dict[str, Any]:
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

    def analyze_track(self, settings: Settings, jobs: List[FileJob], album: Optional[AlbumJob]) -> TrackResult:
        results = TrackResult([])
        try:
            for job in jobs:
                result = FileResult(job.file)
                results.files.append(result)
                try:
                    self._analyze_file(settings, job, album, result)
                except AnalysisCancelled:
                    raise
                except Exception as e:
                    error_msg = f"Unexpected error analyzing {job.filename}: {str(e)}"
                    self.log.error(error_msg)
                    result.errors.append(error_msg)
        except AnalysisCancelled:
            results.cancelled = True
        return results

    def _analyze_file(self, settings: Settings, job: FileJob, album: Optional[AlbumJob], result: FileResult) -> None:
        def error(message: str) -> None:
            self.log.error(message)
            result.errors.append(message)

        try:
            cache_folder = self._generate_cache_folder(settings, job.metadata, job.filename)
        except AnalysisCancelled:
            raise
        except Exception as e:
            error(f"Error generating cache folder for {job.filename}: {e}")
            return

        with ProcessingFile(self, settings, job.filename) as source:
            if not self.analyze_required(settings, source, cache_folder) or not self.parse_required(settings, cache_folder, result.tags):
                error(f"Failed to analyze required models for {job.filename}")
                return

            if settings.analyze_optional:
                if not self.analyze_optional(settings, source, cache_folder) or not self.parse_optional(settings, cache_folder, result.tags):
                    error(f"Failed to analyze optional models for {job.filename}")

        if settings.calculate_replaygain:
            if not self.calculate_loudness(settings, job.filename, cache_folder, album, result.tags):
                error(f"Failed to calculate loudness for {job.filename}")

        if settings.save_fingerprint:
            result.fingerprint = self.build_fingerprint(settings, cache_folder)
            if result.fingerprint is None:
                error(f"Failed to save fingerprint for {job.filename}")

        result.processed = True

    def delete_cache(self, settings: Settings, jobs: List[Tuple[FileJob, bool]]) -> None:
        for job, delete_album in jobs:
            try:
                cache_folder = self._generate_cache_folder(settings, job.metadata, job.filename, create=False)
                if os.path.exists(cache_folder):
                    shutil.rmtree(cache_folder)
                    self.log.debug(f"Deleted cache folder: {cache_folder}")
                album_file = os.path.join(os.path.dirname(cache_folder), ALBUM_LOUDNESS_FILE)
                if delete_album and os.path.exists(album_file):
                    os.remove(album_file)
                    self.log.debug(f"Deleted album loudness cache: {album_file}")
            except Exception as e:
                self.log.error(f"Error deleting cache for {job.filename}: {e}")


def _fingerprint_png(values: List[float]) -> bytes:
    # 95 values plus a 5 value checksum, rendered as an inverted 10x10 grayscale image
    clipped = [min(max(value, 0.0), 1.0) for value in values]

    buf = hashlib.sha256(struct.pack(f"<{len(clipped)}H", *(int(value * 65535) for value in clipped))).digest()
    while len(buf) < 5 * 4:
        buf += hashlib.sha256(buf).digest()
    checksum = [struct.unpack(">I", buf[i * 4:i * 4 + 4])[0] / 0xFFFFFFFF for i in range(5)]

    pixels = bytes(255 - round(value * 255) for value in clipped + checksum)
    width = height = 10

    def _chunk(c_type, data):
        chunk = struct.pack(">I", len(data)) + c_type + data
        crc = zlib.crc32(c_type + data) & 0xFFFFFFFF
        return chunk + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB",
                       width, height,
                       8,    # bit depth
                       0,    # color type = 0 (grayscale)
                       0,    # compression
                       0,    # filter
                       0)    # interlace
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(pixels[y * width:(y + 1) * width])

    return (
        b'\x89PNG\r\n\x1a\n'
        + _chunk(b'IHDR', ihdr)
        + _chunk(b'IDAT', zlib.compress(bytes(raw), level=9))
        + _chunk(b'IEND', b'')
    )


_plugin: Optional[AcousticBrainzNG] = None


def _collect_tracks(objs) -> List[Tuple[Track, Optional[Album]]]:
    # Albums are collected first so that a track selected together with its
    # album gets album loudness. The non-album tracks "album" is a subclass of
    # Album but must not be treated as an album
    tracks: Dict[int, Tuple[Track, Optional[Album]]] = {}
    for item in objs:
        if isinstance(item, Album):
            album = item if type(item) is Album else None
            for track in item.tracks:
                tracks.setdefault(id(track), (track, album))
    for item in objs:
        if isinstance(item, Track):
            tracks.setdefault(id(item), (item, None))
    return list(tracks.values())


def _file_job(file) -> FileJob:
    return FileJob(
        file=file,
        filename=file.filename,
        metadata={
            name: file.metadata.get(name)
            for name in ('musicbrainz_albumartistid', 'musicbrainz_releasegroupid', 'musicbrainz_albumid', 'musicbrainz_recordingid')
        },
    )


def _album_job(album: Album) -> AlbumJob:
    files = []
    key = []
    for track in album.tracks:
        for file in track.files:
            files.append(file.filename)
            key.append(file.metadata.get('musicbrainz_recordingid') or file.filename)
    return AlbumJob(files=files, key=sorted(key))


def _set_statusbar_message(api: PluginApi, message: str, *args) -> None:
    api.tagger.window.set_statusbar_message(message, *args, translate=None)


class AnalysisBatch:
    def __init__(self, num_tracks: int):
        self.num_tracks = num_tracks
        self.current = 0

    def format_progress(self) -> str:
        if self.num_tracks <= 1:
            return ""
        self.current += 1
        return f" ({self.current}/{self.num_tracks})"


class AcousticBrainzNGAction(BaseAction):
    TITLE = "Analyze"
    MENU = (PLUGIN_NAME,)

    def callback(self, objs):
        plugin = _plugin
        if plugin is None:
            return

        tracks_to_process = []
        album_jobs: Dict[int, AlbumJob] = {}
        total_files = 0

        for track, album in _collect_tracks(objs):
            files = [file for file in track.files if file.filename not in plugin.in_flight]
            if not files:
                continue
            if album is not None and id(album) not in album_jobs:
                album_jobs[id(album)] = _album_job(album)
            total_files += len(files)
            tracks_to_process.append((track, album, files))

        if not tracks_to_process:
            return

        settings = plugin.settings()
        plugin.pool.setMaxThreadCount(settings.max_concurrent_analyses)
        batch = AnalysisBatch(len(tracks_to_process))

        if batch.num_tracks == 1:
            track_name = tracks_to_process[0][0].metadata.get('title', 'Unknown Track')
            _set_statusbar_message(self.api, 'Analyzing "%s" with %s...', track_name, PLUGIN_NAME)
        else:
            _set_statusbar_message(self.api, 'Analyzing %i tracks with %s...', batch.num_tracks, PLUGIN_NAME)

        self.api.logger.debug(f"Analyzing {total_files} files from {batch.num_tracks} tracks with {PLUGIN_NAME}")

        for track, album, files in tracks_to_process:
            plugin.in_flight.update(file.filename for file in files)
            thread.run_task(
                partial(plugin.analyze_track, settings, [_file_job(file) for file in files], album_jobs[id(album)] if album else None),
                partial(_analysis_callback, plugin, batch, track, album, files),
                thread_pool=plugin.pool,
            )


def _apply_file_result(result: FileResult) -> None:
    file = result.file
    if file.state == file.State.REMOVED:
        return

    for name, value in result.tags.items():
        file.metadata[name] = value

    if result.fingerprint is not None:
        data, path = result.fingerprint
        images = file.metadata.images
        for i in reversed(range(len(images))):
            if images[i].comment == FINGERPRINT_COMMENT:
                del images[i]
        images.append(CoverArtImage(
            url=QtCore.QUrl.fromLocalFile(path),
            data=data,
            comment=FINGERPRINT_COMMENT,
            types=['other'],
            support_types=True,
        ))
        file.metadata_images_changed.emit()

    file.update()


def _analysis_callback(plugin: AcousticBrainzNG, batch: AnalysisBatch, track: Track, album: Optional[Album], files: list, result: Optional[TrackResult] = None, error=None):
    for file in files:
        plugin.in_flight.discard(file.filename)

    if result is not None and result.cancelled:
        return

    api = plugin.api
    progress = batch.format_progress()
    track_name = track.metadata.get('title', 'Unknown Track')

    if error is None and result:
        for file_result in result.files:
            _apply_file_result(file_result)
        track.update()
        if album:
            album.update()

        processed = sum(1 for file_result in result.files if file_result.processed)
        has_errors = any(file_result.errors for file_result in result.files)

        if processed == len(result.files) and not has_errors:
            if album:
                album_name = album.metadata.get('album', 'Unknown Album')
                _set_statusbar_message(api, 'Successfully analyzed "%s" from "%s"%s.', track_name, album_name, progress)
            else:
                _set_statusbar_message(api, 'Successfully analyzed "%s"%s.', track_name, progress)
        elif processed > 0:
            _set_statusbar_message(api, 'Partially analyzed "%s" with warnings%s.', track_name, progress)
        else:
            _set_statusbar_message(api, 'Failed to analyze "%s"%s.', track_name, progress)
    else:
        error_msg = str(error) if error else "Unknown error"
        api.logger.error(f"Analysis failed for {track_name}: {error_msg}")
        _set_statusbar_message(api, 'Failed to analyze "%s"%s.', track_name, progress)


class AcousticBrainzNGDeleteCacheAction(BaseAction):
    TITLE = "Delete cache"
    MENU = (PLUGIN_NAME,)

    def callback(self, objs):
        plugin = _plugin
        if plugin is None:
            return

        tracks_to_process = _collect_tracks(objs)
        if not tracks_to_process:
            return

        num_tracks = len(tracks_to_process)

        if num_tracks == 1:
            track_name = tracks_to_process[0][0].metadata.get('title', 'Unknown Track')
            _set_statusbar_message(self.api, 'Deleting %s cache for "%s"...', PLUGIN_NAME, track_name)
        else:
            _set_statusbar_message(self.api, 'Deleting %s cache for %i tracks...', PLUGIN_NAME, num_tracks)

        jobs = [(_file_job(file), album is not None) for track, album in tracks_to_process for file in track.files]

        self.api.logger.debug(f"Deleting {PLUGIN_NAME} cache for {len(jobs)} files from {num_tracks} tracks")

        def done(result=None, error=None):
            if num_tracks == 1:
                track_name = tracks_to_process[0][0].metadata.get('title', 'Unknown Track')
                _set_statusbar_message(self.api, 'Deleted %s cache for "%s".', PLUGIN_NAME, track_name)
            else:
                _set_statusbar_message(self.api, 'Deleted %s cache for %i tracks.', PLUGIN_NAME, num_tracks)

        # Computing the cache folder of a file without a recording MBID decodes
        # the whole file, keep that off the main thread
        thread.run_task(partial(plugin.delete_cache, plugin.settings(), jobs), done)


class AcousticBrainzNGOptionsPage(OptionsPage):
    NAME = "acousticbrainz_ng"
    TITLE = PLUGIN_NAME
    PARENT = "plugins"

    # Registers the options so "Restore defaults" works for this page
    OPTIONS = {name: {} for name, _default, _bounds in PLUGIN_OPTIONS}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setup_ui()

    def _create_path_input_layout(self, line_edit: QtWidgets.QLineEdit, browse_callback, check_callback=None) -> QtWidgets.QHBoxLayout:
        layout = QtWidgets.QHBoxLayout()

        browse_button = QtWidgets.QPushButton("Browse", self)
        browse_button.clicked.connect(browse_callback)
        layout.addWidget(line_edit)
        layout.addWidget(browse_button)

        if check_callback:
            check_button = QtWidgets.QPushButton("Check", self)
            check_button.clicked.connect(check_callback)
            layout.addWidget(check_button)

        return layout

    def _path_line_edit(self, name: str, description: str) -> QtWidgets.QLineEdit:
        line_edit = QtWidgets.QLineEdit(self)
        default = _default_path(self.api, name)
        line_edit.setPlaceholderText(f"{description} (default: {default})" if default else description)
        line_edit.setToolTip("Leave empty to use the default")
        return line_edit

    def setup_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        options_group = QtWidgets.QGroupBox("Options", self)
        options_group.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Minimum)
        options_layout = QtWidgets.QVBoxLayout(options_group)

        self.analyze_optional_checkbox = QtWidgets.QCheckBox("Analyze optional MusicNN models", self)
        self.analyze_optional_checkbox.setToolTip("Include optional MusicNN models in the analysis")

        self.save_raw_checkbox = QtWidgets.QCheckBox("Save raw values", self)
        self.save_raw_checkbox.setToolTip("Save raw MusicNN numbers in the metadata")

        self.calculate_replaygain_checkbox = QtWidgets.QCheckBox("Calculate ReplayGain", self)
        self.calculate_replaygain_checkbox.setToolTip("Calculate ReplayGain values for the track and album")

        self.save_fingerprint_checkbox = QtWidgets.QCheckBox("Save fingerprint image", self)
        self.save_fingerprint_checkbox.setToolTip("Save MusicNN data as an image, requires optional MusicNN values")

        self.analyze_optional_checkbox.toggled.connect(self._update_fingerprint_state)

        musicnn_workers_layout = QtWidgets.QHBoxLayout()
        concurrent_analyses_layout = QtWidgets.QHBoxLayout()
        rg_reference_layout = QtWidgets.QHBoxLayout()

        musicnn_workers_label = QtWidgets.QLabel("Max MusicNN processes:", self)
        musicnn_workers_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.musicnn_workers_input = QtWidgets.QSpinBox(self)
        self.musicnn_workers_input.setToolTip("Maximum number of concurrent MusicNN processes")
        self.apply_option_bounds(self.musicnn_workers_input, "max_musicnn_workers")
        self.musicnn_workers_input.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Preferred)

        musicnn_workers_layout.addWidget(musicnn_workers_label)
        musicnn_workers_layout.addStretch()
        musicnn_workers_layout.addWidget(self.musicnn_workers_input)

        concurrent_analyses_label = QtWidgets.QLabel("Max concurrent analyses:", self)
        concurrent_analyses_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.concurrent_analyses_input = QtWidgets.QSpinBox(self)
        self.concurrent_analyses_input.setToolTip("Maximum number of tracks analyzed simultaneously")
        self.apply_option_bounds(self.concurrent_analyses_input, "max_concurrent_analyses")
        self.concurrent_analyses_input.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Preferred)

        concurrent_analyses_layout.addWidget(concurrent_analyses_label)
        concurrent_analyses_layout.addStretch()
        concurrent_analyses_layout.addWidget(self.concurrent_analyses_input)

        rg_reference_label = QtWidgets.QLabel("ReplayGain reference loudness (LUFS):", self)
        rg_reference_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.rg_reference_input = QtWidgets.QSpinBox(self)
        self.rg_reference_input.setToolTip("ReplayGain reference loudness in LUFS")
        self.apply_option_bounds(self.rg_reference_input, "replaygain_reference_loudness")
        self.rg_reference_input.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Preferred)

        self.calculate_replaygain_checkbox.toggled.connect(self.rg_reference_input.setEnabled)

        rg_reference_layout.addWidget(rg_reference_label)
        rg_reference_layout.addStretch()
        rg_reference_layout.addWidget(self.rg_reference_input)

        options_layout.addWidget(self.analyze_optional_checkbox)
        options_layout.addWidget(self.save_raw_checkbox)
        options_layout.addWidget(self.save_fingerprint_checkbox)
        options_layout.addWidget(self.calculate_replaygain_checkbox)
        options_layout.addLayout(concurrent_analyses_layout)
        options_layout.addLayout(musicnn_workers_layout)

        concurrent_processes_layout = QtWidgets.QHBoxLayout()
        concurrent_processes_label = QtWidgets.QLabel("Max concurrent processes:", self)
        concurrent_processes_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.concurrent_processes_display = QtWidgets.QLabel("0", self)
        self.concurrent_processes_display.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.concurrent_processes_display.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)

        concurrent_processes_layout.addWidget(concurrent_processes_label)
        concurrent_processes_layout.addStretch()
        concurrent_processes_layout.addWidget(self.concurrent_processes_display)

        def update_concurrent_processes():
            concurrent_analyses = self.concurrent_analyses_input.value()
            musicnn_workers = self.musicnn_workers_input.value()
            max_processes = (2 * concurrent_analyses) + (concurrent_analyses * musicnn_workers)
            breakdown = f"[(2 x {concurrent_analyses}) feature processes + ({concurrent_analyses} x {musicnn_workers}) MusicNN processes]"
            self.concurrent_processes_display.setText(f"{breakdown} = <span style='font-weight: bold;'>{max_processes}</span>")

        self.concurrent_analyses_input.valueChanged.connect(update_concurrent_processes)
        self.musicnn_workers_input.valueChanged.connect(update_concurrent_processes)

        options_layout.addLayout(rg_reference_layout)

        options_layout.addLayout(concurrent_processes_layout)

        layout.addWidget(options_group)

        paths_group = QtWidgets.QGroupBox("Paths", self)
        paths_group.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Minimum)
        paths_layout = QtWidgets.QVBoxLayout(paths_group)

        # Binaries path
        self.binaries_path_input = self._path_line_edit("binaries_path", "Path to Essentia binaries")
        binaries_layout = self._create_path_input_layout(
            self.binaries_path_input,
            lambda: self._browse_folder(self.binaries_path_input),
            lambda: self._check_binaries(show_success=True)
        )

        # FFmpeg path
        self.ffmpeg_path_input = self._path_line_edit("ffmpeg_path", "Path to FFmpeg")
        ffmpeg_layout = self._create_path_input_layout(
            self.ffmpeg_path_input,
            lambda: self._browse_file(self.ffmpeg_path_input),
            lambda: self._check_binaries(show_success=True)
        )

        # Models path
        self.models_path_input = self._path_line_edit("models_path", "Path to MusicNN models")
        models_layout = self._create_path_input_layout(
            self.models_path_input,
            lambda: self._browse_folder(self.models_path_input),
            lambda: self._check_models(show_success=True, check_optional=True)
        )

        # Cache path
        self.cache_path_input = self._path_line_edit("cache_path", "Path to cache directory")
        cache_layout = self._create_path_input_layout(
            self.cache_path_input,
            lambda: self._browse_folder(self.cache_path_input)
        )

        paths_layout.addWidget(QtWidgets.QLabel("FFmpeg", self))
        paths_layout.addLayout(ffmpeg_layout)
        paths_layout.addWidget(QtWidgets.QLabel("Binaries", self))
        paths_layout.addLayout(binaries_layout)
        paths_layout.addWidget(QtWidgets.QLabel("Models", self))
        paths_layout.addLayout(models_layout)
        paths_layout.addWidget(QtWidgets.QLabel("Cache", self))
        paths_layout.addLayout(cache_layout)

        layout.addWidget(paths_group)

        layout.addStretch()

    def _update_fingerprint_state(self, checked):
        if not checked:
            self.save_fingerprint_checkbox.setChecked(False)
        self.save_fingerprint_checkbox.setEnabled(checked)

    def _check_binaries(self, show_success=False) -> bool:
        binaries_path = _resolve_path(self.api, "binaries_path", self.binaries_path_input.text())
        if not binaries_path or not os.path.exists(binaries_path):
            QtWidgets.QMessageBox.warning(self, "Binaries", "Invalid or empty binaries path.")
            return False

        ffmpeg_path = _resolve_path(self.api, "ffmpeg_path", self.ffmpeg_path_input.text())
        if not ffmpeg_path or not os.path.exists(ffmpeg_path):
            QtWidgets.QMessageBox.warning(self, "Binaries", "Invalid or empty FFmpeg path.")
            return False

        missing_binaries = []
        for binary in REQUIRED_BINARIES:
            if not os.path.exists(os.path.join(binaries_path, _exe(binary))):
                missing_binaries.append(binary)

        try:
            result = subprocess.run(
                [ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            if result.returncode != 0 or "ffmpeg version" not in result.stdout:
                missing_binaries.append("FFmpeg (invalid executable)")
                if result.returncode != 0:
                    self.api.logger.error(f"FFmpeg version check failed with exit code {result.returncode}")
                    if result.stdout:
                        self.api.logger.error(f"FFmpeg stdout: {result.stdout}")
                    if result.stderr:
                        self.api.logger.error(f"FFmpeg stderr: {result.stderr}")
        except Exception as e:
            missing_binaries.append("FFmpeg (unable to execute)")
            self.api.logger.error(f"Exception running FFmpeg version check: {e}")

        if missing_binaries:
            message = "Missing binaries:\n" + "\n".join(f"• {binary}" for binary in missing_binaries)
            QtWidgets.QMessageBox.warning(self, "Binaries", message)
            return False
        else:
            if show_success:
                QtWidgets.QMessageBox.information(self, "Binaries", "All binaries found!")
            return True

    def _check_models(self, show_success=False, check_optional=True) -> bool:
        path = _resolve_path(self.api, "models_path", self.models_path_input.text())
        if not path or not os.path.exists(path):
            QtWidgets.QMessageBox.warning(self, "Models", "Invalid or empty path.")
            return False

        missing_required = []
        for model in REQUIRED_MODELS:
            model_path = os.path.join(path, f"{model[0]}.pb")
            metadata_path = os.path.join(path, f"{model[0]}.json")
            if not os.path.exists(model_path) or not os.path.exists(metadata_path):
                missing_required.append(model[0])

        missing_optional = []
        if check_optional:
            for model in OPTIONAL_MODELS:
                model_path = os.path.join(path, f"{model[0]}.pb")
                metadata_path = os.path.join(path, f"{model[0]}.json")
                if not os.path.exists(model_path) or not os.path.exists(metadata_path):
                    missing_optional.append(model[0])

        if missing_required:
            message = "Note: Model JSON metadata required as well\n\nMissing required models:\n" + "\n".join(f"• {model}" for model in missing_required)
            QtWidgets.QMessageBox.warning(self, "Models", message)
            return False
        elif missing_optional and check_optional:
            message = "Note: Model JSON metadata required as well\n\nMissing optional models:\n" + "\n".join(f"• {model}" for model in missing_optional)
            QtWidgets.QMessageBox.information(self, "Models", message)

        if show_success:
            if missing_optional and check_optional:
                QtWidgets.QMessageBox.information(self, "Models", "All required models found! Some optional models are missing.")
            else:
                QtWidgets.QMessageBox.information(self, "Models", "All models found!")

        return True

    def _browse_folder(self, line_edit: QtWidgets.QLineEdit) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Folder",
            line_edit.text() or os.path.expanduser("~")
        )
        if folder:
            line_edit.setText(folder)

    def _browse_file(self, line_edit: QtWidgets.QLineEdit) -> None:
        file, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select File",
            line_edit.text() or os.path.expanduser("~"),
            "All Files (*)"
        )
        if file:
            line_edit.setText(file)

    def load(self):
        cfg = self.api.plugin_config

        self.analyze_optional_checkbox.setChecked(cfg["analyze_optional"])
        self.save_raw_checkbox.setChecked(cfg["save_raw"])
        self.calculate_replaygain_checkbox.setChecked(cfg["calculate_replaygain"])
        self.rg_reference_input.setEnabled(self.calculate_replaygain_checkbox.isChecked())

        optional_setting = cfg["analyze_optional"]
        self.save_fingerprint_checkbox.setChecked(cfg["save_fingerprint"] and optional_setting)
        self._update_fingerprint_state(optional_setting)

        self.musicnn_workers_input.setValue(cfg["max_musicnn_workers"])
        self.concurrent_analyses_input.setValue(cfg["max_concurrent_analyses"])
        self.rg_reference_input.setValue(cfg["replaygain_reference_loudness"])

        self.binaries_path_input.setText(cfg["binaries_path"])
        self.ffmpeg_path_input.setText(cfg["ffmpeg_path"])
        self.models_path_input.setText(cfg["models_path"])
        self.cache_path_input.setText(cfg["cache_path"])

    def save(self):
        self._check_binaries()
        self._check_models(show_success=False, check_optional=False)

        cfg = self.api.plugin_config

        cfg["analyze_optional"] = self.analyze_optional_checkbox.isChecked()
        cfg["save_raw"] = self.save_raw_checkbox.isChecked()
        cfg["calculate_replaygain"] = self.calculate_replaygain_checkbox.isChecked()
        cfg["save_fingerprint"] = self.save_fingerprint_checkbox.isChecked()

        cfg["max_musicnn_workers"] = self.musicnn_workers_input.value()
        cfg["max_concurrent_analyses"] = self.concurrent_analyses_input.value()
        cfg["replaygain_reference_loudness"] = self.rg_reference_input.value()

        cfg["binaries_path"] = self.binaries_path_input.text().strip()
        cfg["ffmpeg_path"] = self.ffmpeg_path_input.text().strip()
        cfg["models_path"] = self.models_path_input.text().strip()
        cfg["cache_path"] = self.cache_path_input.text().strip()


def _migrate_legacy_settings(api: PluginApi) -> None:
    # Carry over the settings of the Picard 2 version of this plugin. Paths to
    # the bundled binaries, models and cache are not migrated, they pointed to
    # the old plugin install
    if api.plugin_persist["legacy_settings_migrated"]:
        return

    setting = api.global_config.setting
    for name, qtype in LEGACY_OPTION_TYPES.items():
        legacy_name = LEGACY_OPTION_PREFIX + name
        try:
            if legacy_name not in setting or name in api.plugin_config:
                continue
            value = setting.raw_value(legacy_name, qtype=qtype)
            if name == "ffmpeg_path" and not (value and os.path.isfile(value)):
                continue
            api.plugin_config[name] = value
            api.logger.info(f"Migrated setting {legacy_name} = {value!r}")
        except Exception as e:
            api.logger.warning(f"Failed to migrate setting {legacy_name}: {e}")

    api.plugin_persist["legacy_settings_migrated"] = True


def enable(api: PluginApi) -> None:
    global _plugin

    for name, default, bounds in PLUGIN_OPTIONS:
        api.plugin_config.register_option(name, default, bounds=bounds)
    api.plugin_persist.register_option("legacy_settings_migrated", False)
    _migrate_legacy_settings(api)

    _plugin = AcousticBrainzNG(api)
    api.tagger.register_cleanup(_plugin.shutdown)

    api.register_options_page(AcousticBrainzNGOptionsPage)
    api.register_track_action(AcousticBrainzNGAction)
    api.register_album_action(AcousticBrainzNGAction)
    api.register_track_action(AcousticBrainzNGDeleteCacheAction)
    api.register_album_action(AcousticBrainzNGDeleteCacheAction)


def disable() -> None:
    global _plugin

    if _plugin is not None:
        _plugin.shutdown()
        _plugin = None
