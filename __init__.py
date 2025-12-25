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
from functools import partial
from typing import List, Tuple, Dict, Optional
from picard import config, log
from picard.ui.itemviews import (
    BaseAction,
    register_track_action,
    register_album_action,
)
from picard.track import Track
from picard.album import Album
from picard.ui.options import OptionsPage, register_options_page
from picard.util import thread
from picard.coverart.image import CoverArtImage
from PyQt5 import QtWidgets, QtCore

_analysis_semaphore = None
_current_max_concurrent = 0

def _get_analysis_semaphore():
    global _analysis_semaphore, _current_max_concurrent
    
    max_concurrent = config.setting["acousticbrainz_ng_max_concurrent_analyses"] or 2
    
    if _analysis_semaphore is None or _current_max_concurrent != max_concurrent:
        _analysis_semaphore = threading.Semaphore(max_concurrent)
        _current_max_concurrent = max_concurrent
        log.debug(f"Created analysis semaphore with limit: {max_concurrent}")
    
    return _analysis_semaphore

from .constants import *

class AcousticBrainzNG:
    def __init__(self):
        pass

    @staticmethod
    def _get_binary_path(binary_name: str, binaries_path: str) -> str:
        binary_path = os.path.join(binaries_path, binary_name)
        if os.name == 'nt':  # Windows
            binary_path += '.exe'
        return binary_path

    def _get_binary_paths(self) -> Tuple[str, str, str]:
        binaries_path = config.setting["acousticbrainz_ng_binaries_path"]
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

    def _run_musicnn_models(self, models: List[Tuple[str, str]], musicnn_binary_path: str, file: str, output_path: str) -> bool:
        models_path = config.setting["acousticbrainz_ng_models_path"]
        if not models_path:
            log.error("Models path not configured")
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
                
                result = subprocess.run(
                    [musicnn_binary_path, model_path, file, output_file_path],
                    capture_output=True,
                    text=True,
                    env=ENV,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                
                if result.returncode != 0:
                    success_results[model_name] = False
                    log.error(f"MusicNN binary {musicnn_binary_path} failed for model {model_name} on file {file} with exit code {result.returncode}")
                    if result.stdout:
                        log.error(f"MusicNN stdout: {result.stdout}")
                    if result.stderr:
                        log.error(f"MusicNN stderr: {result.stderr}")
                else:
                    success_results[model_name] = True
            except FileNotFoundError as e:
                success_results[model_name] = False
                log.error(f"Model {model_name} not found: {e}")
            except Exception as e:
                success_results[model_name] = False
                log.error(f"Error processing model {model_name}: {e}")

        max_workers = config.setting["acousticbrainz_ng_max_musicnn_workers"] or 4
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(run_musicnn_model, model) for model in models]
            concurrent.futures.wait(futures)
        
        return all(success_results.get(model[0], False) for model in models)

    def analyze_required(self, metadata: Dict, file: str) -> bool:        
        if not self._check_binaries():
            log.error("Essentia binaries not found")
            return False

        if not self._check_required_models():
            log.error("Required models not found")
            return False
        
        try:
            musicnn_binary_path, rhythm_binary_path, key_binary_path = self._get_binary_paths()
        except (ValueError, FileNotFoundError) as e:
            log.error(str(e))
            return False
    
        try:
            output_path = self._generate_cache_folder(metadata, file)
            if not output_path:
                log.error("Failed to generate cache folder path")
                return False
        except Exception as e:
            log.error(f"Error generating cache folder: {e}")
            return False

        processing_file, temp_file = self._get_processing_file(file)
        
        try:
            rhythm_success = True
            def run_rhythm():
                nonlocal rhythm_success
                if os.path.exists(os.path.join(output_path, "rhythm.yaml")):
                    return

                rhythm_proc = subprocess.run(
                    [rhythm_binary_path, processing_file],
                    capture_output=True,
                    text=True,
                    env=ENV,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                
                if rhythm_proc.returncode != 0:
                    rhythm_success = False
                    log.error(f"Rhythm binary {rhythm_binary_path} failed on file {processing_file} with exit code {rhythm_proc.returncode}")
                    if rhythm_proc.stdout:
                        log.error(f"Rhythm stdout: {rhythm_proc.stdout}")
                    if rhythm_proc.stderr:
                        log.error(f"Rhythm stderr: {rhythm_proc.stderr}")
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

                    out_file = os.path.join(output_path, "rhythm.yaml")
                    with open(out_file, "w", encoding="utf-8") as f:
                        f.write(yaml_str)
                except Exception as e:
                    rhythm_success = False
                    log.error(f"Failed to extract/save rhythm.yaml from rhythm binary stdout: {e}")
                    if rhythm_proc.stdout:
                        log.error(f"Rhythm stdout: {rhythm_proc.stdout}")
                    if rhythm_proc.stderr:
                        log.error(f"Rhythm stderr: {rhythm_proc.stderr}")
                    return
            
            key_success = True
            def run_key():
                nonlocal key_success
                if os.path.exists(os.path.join(output_path, "key.yaml")):
                    return

                key_proc = subprocess.run(
                    [key_binary_path, processing_file, os.path.join(output_path, "key.yaml")],
                    capture_output=True,
                    text=True,
                    env=ENV,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )

                if key_proc.returncode != 0:
                    key_success = False
                    log.error(f"Key binary {key_binary_path} failed on file {processing_file} with exit code {key_proc.returncode}")
                    if key_proc.stdout:
                        log.error(f"Key stdout: {key_proc.stdout}")
                    if key_proc.stderr:
                        log.error(f"Key stderr: {key_proc.stderr}")
                    return

            rhythm_thread = threading.Thread(target=run_rhythm)
            rhythm_thread.start()

            key_thread = threading.Thread(target=run_key)
            key_thread.start()

            musicnn_success = self._run_musicnn_models(REQUIRED_MODELS, musicnn_binary_path, processing_file, output_path)
            rhythm_thread.join()
            key_thread.join()

            return rhythm_success and key_success and musicnn_success
        finally:
            if temp_file:
                self._cleanup_temp_file(temp_file)

    def analyze_optional(self, metadata: Dict, file: str) -> bool:        
        if not self._check_binaries():
            log.error("Essentia binaries not found")
            return False

        if not self._check_optional_models():
            log.error("Optional models not found")
            return False
        
        try:
            musicnn_binary_path = self._get_binary_paths()[0]
        except (ValueError, FileNotFoundError) as e:
            log.error(str(e))
            return False
    
        try:
            output_path = self._generate_cache_folder(metadata, file)
            if not output_path:
                log.error("Failed to generate cache folder path")
                return False
        except Exception as e:
            log.error(f"Error generating cache folder: {e}")
            return False

        processing_file, temp_file = self._get_processing_file(file)
        
        try:
            return self._run_musicnn_models(OPTIONAL_MODELS, musicnn_binary_path, processing_file, output_path)
        finally:
            if temp_file:
                self._cleanup_temp_file(temp_file)
    
    def parse_required(self, metadata: Dict, file: str) -> bool:
        if not self._check_required_models():
            log.error("Required models not found")
            return False
        
        models_path = config.setting["acousticbrainz_ng_models_path"]
        if not models_path:
            log.error("Models path not configured")
            return False

        try:
            output_path = self._generate_cache_folder(metadata, file)
            if not output_path:
                log.error("Failed to generate cache folder path")
                return False
        except Exception as e:
            log.error(f"Error generating cache folder: {e}")
            return False
        
        moods = []
        tags = []
        
        for model, output in REQUIRED_MODELS:
            model_json_path = os.path.join(models_path, f"{model}.json")
            if not os.path.exists(model_json_path):
                log.error(f"Model JSON metadata not found: {model_json_path}")
                return False
            
            output_file_path = os.path.join(output_path, f"{output}.json")
            if not os.path.exists(output_file_path):
                log.error(f"Output file not found: {output_file_path}")
                return False
            
            output_data = {}
            model_metadata = {}

            try:
                with open(model_json_path, 'r', encoding='utf-8') as f:
                    model_metadata = json.load(f)
                
                with open(output_file_path, 'r', encoding='utf-8') as f:
                    output_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as e:
                log.error(f"Error reading model or output file: {e}")
                return False

            if not output_data["predictions"] or not output_data["predictions"]["mean"]:
                log.error(f"No predictions found in output data for {model}")
                return False
            
            if not model_metadata["classes"] or len(model_metadata["classes"]) != len(output_data["predictions"]["mean"]):
                log.error(f"No or invalid classes defined in model metadata for {model}")
                return False

            if len(model_metadata["classes"]) == 2:
                values = output_data["predictions"]["mean"]
                max_index = values.index(max(values))

                mood_class = model_metadata["classes"][max_index]
                mood_formatted = self._format_class(mood_class)
                moods.append(mood_formatted)
            elif model == REQUIRED_MODELS[0][0]:
                values = output_data["predictions"]["mean"]
                class_value_pairs = [
                    {"class": class_name, "value": value}
                    for class_name, value in zip(model_metadata["classes"], values)
                ]

                top5 = sorted(class_value_pairs, key=lambda x: x["value"], reverse=True)[:5]

                for item in top5:
                    formatted_tag = item["class"][0].upper() + item["class"][1:] if item["class"] else ""
                    tags.append(formatted_tag)
            
            if config.setting["acousticbrainz_ng_save_raw"]:
                for i in range(len(output_data["predictions"]["mean"])):
                    metadata[f"ab:hi:{output}:{model_metadata['classes'][i].replace('non', 'not').replace('_', ' ').lower()}"] = output_data["predictions"]["mean"][i]

        metadata['mood'] = moods
        metadata['tags'] = tags
        
        rhythm_data = {}
        rhythm_yaml_path = os.path.join(output_path, "rhythm.yaml")

        key_data = {}
        key_yaml_path = os.path.join(output_path, "key.yaml")

        if os.path.exists(rhythm_yaml_path):
            with open(rhythm_yaml_path, 'r', encoding='utf-8') as f:
                loaded = yaml.safe_load(f)

            if not isinstance(loaded, dict):
                log.error("Invalid rhythm YAML format: expected a mapping at the top level")
                return False

            rhythm_data = loaded
        else:
            log.error(f"Rhythm YAML file not found: {rhythm_yaml_path}")
            return False

        if os.path.exists(key_yaml_path):
            with open(key_yaml_path, 'r', encoding='utf-8') as f:
                loaded = yaml.safe_load(f)
            if not isinstance(loaded, dict):
                log.error("Invalid key YAML format: expected a mapping at the top level")
                return False

            key_data = loaded
        else:
            log.error(f"Key YAML file not found: {key_yaml_path}")
            return False

        try:
            metadata["bpm"] = int(round(rhythm_data["bpm"]))
            metadata["key"] = "o" if key_data["tonal"]["key_scale"] == "off" else f"{key_data['tonal']['key']}{'m' if key_data['tonal']['key_scale'] == 'minor' else ''}"

            if config.setting["acousticbrainz_ng_save_raw"]:
                metadata["ab:lo:tonal:key_scale"] = key_data["tonal"]["key_scale"]
                metadata["ab:lo:tonal:key_key"] = key_data["tonal"]["key"]
            
            return True
        except Exception as e:
            log.error(f"Error processing feature data: {e}")
            return False
    
    def parse_optional(self, metadata: Dict, file: str) -> bool:
        if not self._check_optional_models():
            log.error("Optional models not found")
            return False
        
        models_path = config.setting["acousticbrainz_ng_models_path"]
        if not models_path:
            log.error("Models path not configured")
            return False

        try:
            output_path = self._generate_cache_folder(metadata, file)
            if not output_path:
                log.error("Failed to generate cache folder path")
                return False
        except Exception as e:
            log.error(f"Error generating cache folder: {e}")
            return False
        
        for model, output in OPTIONAL_MODELS:
            model_json_path = os.path.join(models_path, f"{model}.json")
            if not os.path.exists(model_json_path):
                log.error(f"Model JSON metadata not found: {model_json_path}")
                return False
            
            output_file_path = os.path.join(output_path, f"{output}.json")
            if not os.path.exists(output_file_path):
                log.error(f"Output file not found: {output_file_path}")
                return False
            
            output_data = {}
            model_metadata = {}

            try:
                with open(model_json_path, 'r', encoding='utf-8') as f:
                    model_metadata = json.load(f)
                
                with open(output_file_path, 'r', encoding='utf-8') as f:
                    output_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as e:
                log.error(f"Error reading model or output file: {e}")
                return False

            if not output_data["predictions"] or not output_data["predictions"]["mean"]:
                log.error(f"No predictions found in output data for {model}")
                return False
            
            if not model_metadata["classes"] or len(model_metadata["classes"]) != len(output_data["predictions"]["mean"]):
                log.error(f"No or invalid classes defined in model metadata for {model}")
                return False
            
            if config.setting["acousticbrainz_ng_save_raw"]:
                for i in range(len(output_data["predictions"]["mean"])):
                    metadata[f"ab:hi:{output}:{model_metadata['classes'][i].replace('non', 'not').replace('_', ' ').lower()}"] = output_data["predictions"]["mean"][i]

        return True
    
    def save_fingerprint(self, metadata: Dict, file_path: str, file_obj) -> bool:
        if not self._check_optional_models():
            log.error("Optional models not found")
            return False
        
        models_path = config.setting["acousticbrainz_ng_models_path"]
        if not models_path:
            log.error("Models path not configured")
            return False

        try:
            output_path = self._generate_cache_folder(metadata, file_path)
            if not output_path:
                log.error("Failed to generate cache folder path")
                return False
        except Exception as e:
            log.error(f"Error generating cache folder: {e}")
            return False
        
        try:
            output_path = self._generate_cache_folder(metadata, file_path)
            if not output_path:
                log.error("Failed to generate cache folder path")
                return False
        except Exception as e:
            log.error(f"Error generating cache folder: {e}")
            return False

        fingerprint_data = []

        for key, value in metadata.items():
            if key.lower().startswith("ab:hi:"):
                try:
                    float_value = float(value)
                    if 0 <= float_value <= 1:
                        fingerprint_data.append(float_value)
                except (ValueError, TypeError):
                    continue

        if not fingerprint_data:
            log.error("No valid fingerprint data found in metadata")
            return False
        
        if len(fingerprint_data) != 95:
            log.error(f"Fingerprint expected exactly 95 values, got {len(fingerprint_data)}")
            return False

        fingerprint_file = os.path.join(output_path, "fingerprint.png")
        
        try:
            try:
                import numpy as _np
            except Exception as e:
                log.error(f"numpy is required to generate fingerprint PNG: {e}")
                return False

            def _checksum_floats(values, n=5):
                arr = _np.clip(_np.asarray(values, dtype=float).flatten(), 0.0, 1.0)
                b = (arr * 65535).astype(_np.uint16).tobytes()
                buf = hashlib.sha256(b).digest()

                while len(buf) < n * 4:
                    buf += hashlib.sha256(buf).digest()

                out = []

                for i in range(n):
                    start = i * 4
                    u = struct.unpack(">I", buf[start:start+4])[0]
                    out.append(u / 0xFFFFFFFF)

                return _np.array(out, dtype=float)

            def _to_grayscale_uint8(arr):
                a = _np.clip(_np.asarray(arr, dtype=float), 0.0, 1.0)
                return (255 - _np.round(a * 255)).astype(_np.uint8)

            def _png_write_grayscale(path, img8):
                if img8.ndim != 2 or img8.dtype != _np.uint8:
                    raise ValueError("img8 must be a 2D numpy array of dtype uint8")
                height, width = int(img8.shape[0]), int(img8.shape[1])

                def _chunk(c_type, data):
                    chunk = struct.pack(">I", len(data)) + c_type + data
                    crc = zlib.crc32(c_type + data) & 0xFFFFFFFF
                    return chunk + struct.pack(">I", crc)

                png_sig = b'\x89PNG\r\n\x1a\n'
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
                    raw.extend(img8[y].tobytes())
                comp = zlib.compress(bytes(raw), level=9)

                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(png_sig)
                    f.write(_chunk(b'IHDR', ihdr))
                    f.write(_chunk(b'IDAT', comp))
                    f.write(_chunk(b'IEND', b''))

            v = _np.clip(_np.asarray(fingerprint_data, dtype=float).flatten(), 0.0, 1.0)
            base = _np.zeros(100, dtype=float)
            base[:95] = v
            base[95:] = _checksum_floats(v, n=5)
            base = base.reshape((10, 10))

            img8 = _to_grayscale_uint8(base)

            _png_write_grayscale(fingerprint_file, img8)
            
            fingerprint_url = f"file://{fingerprint_file.replace(os.sep, '/')}"
            
            with open(fingerprint_file, "rb") as f:
                fingerprint_data_bytes = f.read()
            
            cover_art_image = CoverArtImage(url=fingerprint_url, data=fingerprint_data_bytes, comment=f"{PLUGIN_NAME} fingerprint", types=['other'], support_types=True)

            file_obj.metadata.images.append(cover_art_image)

            file_obj.metadata_images_changed.emit()

        except Exception as e:
            log.error(f"Failed to create fingerprint PNG: {e}")
            return False

        return True

    @staticmethod
    def _format_class(class_name: str) -> str:
        return class_name.replace("non", "not").replace("_", " ").capitalize()

    def _generate_cache_folder(self, metadata: Dict, file_path: str) -> str:
        cache_base = config.setting["acousticbrainz_ng_cache_path"]
        if not cache_base:
            raise ValueError("Cache path not configured")

        release_artist_mbid = metadata.get('musicbrainz_albumartistid', 'NO_MBID')
        release_group_mbid = metadata.get('musicbrainz_releasegroupid', 'NO_MBID')
        release_mbid = metadata.get('musicbrainz_albumid', 'NO_MBID')
        recording_mbid = metadata.get('musicbrainz_recordingid')
        
        if not recording_mbid:
            recording_mbid = self._get_audio_hash(file_path)

        cache_folder = os.path.join(
            str(cache_base),
            str(release_artist_mbid),
            str(release_group_mbid),
            str(release_mbid),
            str(recording_mbid)
        )

        os.makedirs(cache_folder, exist_ok=True)
        
        return cache_folder
    
    def _get_audio_hash(self, file_path: str) -> str:
        try:
            binaries_path = config.setting["acousticbrainz_ng_binaries_path"]
            if not binaries_path:
                raise ValueError("Binaries path not configured")
                
            binary_path = self._get_binary_path("streaming_md5", binaries_path)
            
            result = subprocess.run(
                [binary_path, file_path],
                capture_output=True,
                text=True,
                env=ENV,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line.startswith('MD5:'):
                        return line.split('MD5:')[1].strip()
            else:
                log.error(f"MD5 binary {binary_path} failed on file {file_path} with exit code {result.returncode}")
                if result.stdout:
                    log.error(f"MD5 stdout: {result.stdout}")
                if result.stderr:
                    log.error(f"MD5 stderr: {result.stderr}")
            
            log.error(f"Failed to calculate audio hash for file {file_path}: MD5 not found in output")
            
        except Exception as e:
            log.error(f"Error calculating audio hash: {e}")

        return f"fallback_{hashlib.md5(file_path.encode('utf-8')).hexdigest()}"
    
    def _check_binaries(self) -> bool:
        path = config.setting["acousticbrainz_ng_binaries_path"]
        
        if not path or not os.path.exists(path):
            return False
        
        for binary in REQUIRED_BINARIES:
            binary_path = self._get_binary_path(binary, path)
            if not os.path.exists(binary_path):
                return False
        
        return True

    def _check_models(self, models: List[Tuple[str, str]]) -> bool:
        path = config.setting["acousticbrainz_ng_models_path"]
        
        if not path or not os.path.exists(path):
            return False
        
        for model in models:
            model_path = os.path.join(path, f"{model[0]}.pb")
            if not os.path.exists(model_path):
                return False
        
        return True

    def _check_required_models(self) -> bool:
        return self._check_models(REQUIRED_MODELS)

    def _check_optional_models(self) -> bool:
        return self._check_models(OPTIONAL_MODELS)

    def _is_opus_file(self, file_path: str) -> bool:
        return file_path.lower().endswith('.opus')
    
    def _get_audio_channel_count(self, file_path: str) -> int:
        try:
            ffmpeg_path = config.setting["acousticbrainz_ng_ffmpeg_path"]
            if not ffmpeg_path:
                log.warning("FFmpeg path not configured, assuming stereo")
                return 2
            
            ffprobe_path = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe" + (".exe" if os.name == 'nt' else ""))
            
            if not os.path.exists(ffprobe_path):
                probe_proc = subprocess.run(
                    [ffmpeg_path, "-hide_banner", "-i", file_path],
                    capture_output=True,
                    text=True,
                    env=ENV,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )

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
                probe_proc = subprocess.run(
                    [ffprobe_path, "-v", "error", "-select_streams", "a:0", 
                     "-show_entries", "stream=channels", "-of", "default=noprint_wrappers=1:nokey=1", file_path],
                    capture_output=True,
                    text=True,
                    env=ENV,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                
                if probe_proc.returncode == 0 and probe_proc.stdout.strip():
                    try:
                        return int(probe_proc.stdout.strip())
                    except ValueError:
                        pass
                
                return 2
                
        except Exception as e:
            log.warning(f"Error detecting audio channels for {file_path}: {e}, assuming stereo")
            return 2
    
    def _create_stereo_downmix(self, file_path: str) -> Optional[str]:
        try:
            ffmpeg_path = config.setting["acousticbrainz_ng_ffmpeg_path"]
            if not ffmpeg_path:
                log.error("FFmpeg path not configured, cannot downmix")
                return None

            file_ext = os.path.splitext(file_path)[1]
            temp_dir = tempfile.mkdtemp(prefix="acousticbrainz_ng_")
            temp_file = os.path.join(temp_dir, f"stereo_downmix{file_ext}")
            
            log.debug(f"Downmixing multichannel audio to stereo: {file_path} -> {temp_file}")

            downmix_proc = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-i", file_path, "-ac", "2", "-y", temp_file],
                capture_output=True,
                text=True,
                env=ENV,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if downmix_proc.returncode != 0:
                log.error(f"FFmpeg downmix failed for {file_path} with exit code {downmix_proc.returncode}")
                if downmix_proc.stderr:
                    log.error(f"FFmpeg stderr: {downmix_proc.stderr}")
                try:
                    shutil.rmtree(temp_dir)
                except Exception:
                    pass
                return None
            
            if os.path.exists(temp_file):
                log.debug(f"Successfully created stereo downmix: {temp_file}")
                return temp_file
            else:
                log.error(f"Stereo downmix file was not created: {temp_file}")
                try:
                    shutil.rmtree(temp_dir)
                except Exception:
                    pass
                return None
                
        except Exception as e:
            log.error(f"Error creating stereo downmix for {file_path}: {e}")
            return None
    
    def _cleanup_temp_file(self, temp_file_path: str) -> None:
        try:
            if temp_file_path and os.path.exists(temp_file_path):
                temp_dir = os.path.dirname(temp_file_path)
                shutil.rmtree(temp_dir)
                log.debug(f"Cleaned up temporary directory: {temp_dir}")
        except Exception as e:
            log.warning(f"Failed to clean up temporary file {temp_file_path}: {e}")
    
    def _get_processing_file(self, file_path: str) -> Tuple[str, Optional[str]]:
        channels = self._get_audio_channel_count(file_path)
        
        if channels > 2:
            log.info(f"File {file_path} has {channels} channels, creating stereo downmix for processing")
            temp_file = self._create_stereo_downmix(file_path)
            if temp_file:
                return (temp_file, temp_file)
            else:
                log.warning(f"Failed to create stereo downmix for {file_path}, attempting to process original")
                return (file_path, None)
        
        return (file_path, None)
    
    def calculate_track_loudness(self, file_path: str) -> Dict:
        try:
            ffmpeg_path = config.setting["acousticbrainz_ng_ffmpeg_path"]
            if not ffmpeg_path:
                raise ValueError("FFmpeg path not configured")
            
            replaygain_proc = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-i", file_path, "-af", f"loudnorm=I={config.setting['acousticbrainz_ng_replaygain_reference_loudness']}:print_format=json", "-f", "null", "-"],
                capture_output=True,
                text=True,
                env=ENV,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if replaygain_proc.returncode != 0:
                log.error(f"FFmpeg failed for ReplayGain LUFS calculation on file {file_path} with exit code {replaygain_proc.returncode}")
                if replaygain_proc.stdout:
                    log.error(f"FFmpeg stdout: {replaygain_proc.stdout}")
                if replaygain_proc.stderr:
                    log.error(f"FFmpeg stderr: {replaygain_proc.stderr}")
                return {}
            
            replaygain_log = replaygain_proc.stderr or replaygain_proc.stdout
            replaygain_log = "\n".join((replaygain_log or "").splitlines()[-15:])
            
            replaygain_match = re.search(r'\{.*?\}', replaygain_log, re.S)
            replaygain_matches = re.findall(r'\{.*?\}', replaygain_log, re.S) if not replaygain_match else None
            replaygain_json_text = replaygain_match.group(0) if replaygain_match else (replaygain_matches[0] if replaygain_matches else None)
            
            replaygain_gain = None
            replaygain_peak = None
            replaygain_range = None
            
            replaygain_lufs_result: dict | None = None
            
            if replaygain_json_text:
                try:
                    replaygain_lufs_result = json.loads(replaygain_json_text)
                except json.JSONDecodeError:
                    if replaygain_matches:
                        try:
                            replaygain_lufs_result = json.loads(replaygain_matches[-1])
                        except Exception:
                            replaygain_lufs_result = None

            input_i = replaygain_lufs_result.get('input_i') if replaygain_lufs_result else None
            input_tp = replaygain_lufs_result.get('input_tp') if replaygain_lufs_result else None
            input_lra = replaygain_lufs_result.get('input_lra') if replaygain_lufs_result else None

            input_i_val = None
            input_tp_val = None
            input_lra_val = None
            
            try:
                if input_i is not None:
                    input_i_val = float(input_i)
            except (TypeError, ValueError):
                input_i_val = None

            try:
                if input_tp is not None:
                    input_tp_val = float(input_tp)
            except (TypeError, ValueError):
                input_tp_val = None
            
            try:
                if input_lra is not None:
                    input_lra_val = float(input_lra)
            except (TypeError, ValueError):
                input_lra_val = None

            if input_i_val is not None and math.isfinite(input_i_val):
                replaygain_gain = f"{(config.setting['acousticbrainz_ng_replaygain_reference_loudness'] or -18) - input_i_val:.2f}"

            if input_tp_val is not None and math.isfinite(input_tp_val):
                replaygain_peak = f"{10 ** (input_tp_val / 20):.6f}"

            if input_lra_val is not None and math.isfinite(input_lra_val):
                replaygain_range = f"{input_lra_val:.2f}"
            
            result: Dict = {
                "replaygain_track_gain": replaygain_gain,
                "replaygain_track_peak": replaygain_peak,
                "replaygain_track_range": replaygain_range,
                "replaygain_reference_loudness": f"{(config.setting['acousticbrainz_ng_replaygain_reference_loudness'] or -18):.2f}"
            }
            
            r128_proc = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-i", file_path, "-af", "loudnorm=I=-23:print_format=json", "-f", "null", "-"],
                capture_output=True,
                text=True,
                env=ENV,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if r128_proc.returncode != 0:
                log.error(f"FFmpeg failed for R128 calculation on file {file_path} with exit code {r128_proc.returncode}")
                if r128_proc.stdout:
                    log.error(f"FFmpeg stdout: {r128_proc.stdout}")
                if r128_proc.stderr:
                    log.error(f"FFmpeg stderr: {r128_proc.stderr}")
                return result

            r128_log = r128_proc.stderr or r128_proc.stdout
            r128_log = "\n".join((r128_log or "").splitlines()[-15:])

            r128_match = re.search(r'\{.*?\}', r128_log, re.S)
            r128_matches = re.findall(r'\{.*?\}', r128_log, re.S) if not r128_match else None
            r128_json_text = r128_match.group(0) if r128_match else (r128_matches[0] if r128_matches else None)

            r128_track_gain = None

            r128_data: dict | None = None
            
            if r128_json_text:
                try:
                    r128_data = json.loads(r128_json_text)
                except json.JSONDecodeError:
                    if r128_matches:
                        try:
                            r128_data = json.loads(r128_matches[-1])
                        except Exception:
                            r128_data = None

            r128_input_i = r128_data.get('input_i') if r128_data else None

            r128_input_i_val = None
            try:
                if r128_input_i is not None:
                    r128_input_i_val = int(r128_input_i)
            except (TypeError, ValueError):
                r128_input_i_val = None

            if r128_input_i_val is not None and math.isfinite(r128_input_i_val):
                r128_gain_db = -23 - r128_input_i_val
                r128_track_gain = int(round(r128_gain_db * 256))
                
                if r128_track_gain < -32768:
                    r128_track_gain = -32768
                elif r128_track_gain > 32767:
                    r128_track_gain = 32767
            
            result["r128_track_gain"] = r128_track_gain
            
            return result
            
        except Exception as e:
            log.error(f"Error calculating track loudness: {e}")
            return {}

    def calculate_album_loudness(self, album_track_files: List[str]) -> Dict:
        try:
            if len(album_track_files) == 0:
                return {}
            elif len(album_track_files) == 1:
                return {}
            
            ffmpeg_path = config.setting["acousticbrainz_ng_ffmpeg_path"]
            if not ffmpeg_path:
                raise ValueError("FFmpeg path not configured")
            
            album_track_files.sort()
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as concat_file:
                for audio_file in album_track_files:
                    escaped_file = audio_file.replace("'", "'\\''")
                    concat_file.write(f"file '{escaped_file}'\n")
                concat_file_path = concat_file.name
            
            try:
                album_replaygain_proc = subprocess.run(
                    [ffmpeg_path, "-hide_banner", "-f", "concat", "-safe", "0", "-i", concat_file_path, 
                     "-vn", "-af", f"loudnorm=I={config.setting['acousticbrainz_ng_replaygain_reference_loudness']}:print_format=json", "-f", "null", "-"],
                    capture_output=True,
                    text=True,
                    env=ENV,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                
                if album_replaygain_proc.returncode != 0:
                    log.error(f"FFmpeg failed for album ReplayGain calculation on {len(album_track_files)} files with exit code {album_replaygain_proc.returncode}")
                    log.error(f"Album files: {', '.join(album_track_files)}")
                    if album_replaygain_proc.stdout:
                        log.error(f"FFmpeg stdout: {album_replaygain_proc.stdout}")
                    if album_replaygain_proc.stderr:
                        log.error(f"FFmpeg stderr: {album_replaygain_proc.stderr}")
                    return {}

                album_replaygain_log = album_replaygain_proc.stderr or album_replaygain_proc.stdout

                album_replaygain_match = re.search(r'\{.*?\}', album_replaygain_log, re.S)
                album_replaygain_matches = re.findall(r'\{.*?\}', album_replaygain_log, re.S) if not album_replaygain_match else None
                album_replaygain_json_text = album_replaygain_match.group(0) if album_replaygain_match else (album_replaygain_matches[0] if album_replaygain_matches else None)

                album_gain = None
                album_peak = None
                album_range = None
                
                loudnorm_data: dict | None = None
                
                if album_replaygain_json_text:
                    try:
                        loudnorm_data = json.loads(album_replaygain_json_text)
                    except json.JSONDecodeError:
                        if album_replaygain_matches:
                            try:
                                loudnorm_data = json.loads(album_replaygain_matches[-1])
                            except Exception:
                                loudnorm_data = None
                
                input_i = loudnorm_data.get('input_i') if loudnorm_data else None
                input_tp = loudnorm_data.get('input_tp') if loudnorm_data else None
                input_lra = loudnorm_data.get('input_lra') if loudnorm_data else None
                
                try:
                    if input_i:
                        input_i_val = float(input_i)
                except (TypeError, ValueError):
                    input_i_val = None
                
                try:
                    if input_tp:
                        input_tp_val = float(input_tp)
                except (TypeError, ValueError):
                    input_tp_val = None
                
                try:
                    if input_lra:
                        input_lra_val = float(input_lra)
                except (TypeError, ValueError):
                    input_lra_val = None

                if input_i_val is not None and math.isfinite(input_i_val):
                    album_gain = f"{(config.setting['acousticbrainz_ng_replaygain_reference_loudness'] or -18) - input_i_val:.2f}"

                if input_tp_val is not None and math.isfinite(input_tp_val):
                    album_peak = f"{10 ** (input_tp_val / 20):.6f}"

                if input_lra_val is not None and math.isfinite(input_lra_val):
                    album_range = f"{input_lra_val:.2f}"

                result: Dict = {
                    "replaygain_album_gain": album_gain,
                    "replaygain_album_peak": album_peak,
                    "replaygain_album_range": album_range
                }
                
                album_r128_proc = subprocess.run(
                    [ffmpeg_path, "-hide_banner", "-f", "concat", "-safe", "0", "-i", concat_file_path,
                     "-vn", "-af", "loudnorm=I=-23:print_format=json", "-f", "null", "-"],
                    capture_output=True,
                    text=True,
                    env=ENV,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                
                if album_r128_proc.returncode != 0:
                    log.error(f"FFmpeg failed for album R128 calculation on {len(album_track_files)} files with exit code {album_r128_proc.returncode}")
                    log.error(f"Album files: {', '.join(album_track_files)}")
                    if album_r128_proc.stdout:
                        log.error(f"FFmpeg stdout: {album_r128_proc.stdout}")
                    if album_r128_proc.stderr:
                        log.error(f"FFmpeg stderr: {album_r128_proc.stderr}")
                    return result

                album_r128_log = album_r128_proc.stderr or album_r128_proc.stdout
                
                album_r128_match = re.search(r'\{.*?\}', album_r128_log, re.S)
                album_r128_matches = re.findall(r'\{.*?\}', album_r128_log, re.S) if not album_r128_match else None
                album_r128_json_text = album_r128_match.group(0) if album_r128_match else (album_r128_matches[0] if album_r128_matches else None)
                
                r128_album_gain = None
                
                r128_data: dict | None = None
                
                if album_r128_json_text:
                    try:
                        r128_data = json.loads(album_r128_json_text)
                    except json.JSONDecodeError:
                        if album_r128_matches:
                            try:
                                r128_data = json.loads(album_r128_matches[-1])
                            except Exception:
                                r128_data = None
                
                r128_input_i = r128_data.get('input_i') if r128_data else None
                
                try:
                    if r128_input_i:
                        r128_input_i_val = int(r128_input_i)
                except (TypeError, ValueError):
                    r128_input_i_val = None

                if r128_input_i_val is not None and math.isfinite(r128_input_i_val):
                    r128_gain_db = -23 - r128_input_i_val
                    r128_album_gain = int(round(r128_gain_db * 256))
                    
                    if r128_album_gain < -32768:
                        r128_album_gain = -32768
                    elif r128_album_gain > 32767:
                        r128_album_gain = 32767

                result["r128_album_gain"] = r128_album_gain
                
                return result
            
            finally:
                os.unlink(concat_file_path)
                
        except Exception as e:
            log.error(f"Error calculating album loudness: {e}")
            return {}

    def calculate_loudness(self, metadata: Dict, file_path: str, album: Optional[Album] = None) -> bool:
        try:
            cache_folder = self._generate_cache_folder(metadata, file_path)
            loudness_file = os.path.join(cache_folder, f"loudness_{config.setting['acousticbrainz_ng_replaygain_reference_loudness'] or -18}.json")

            if os.path.exists(loudness_file):
                return True

            track_loudness = self.calculate_track_loudness(file_path)
            
            if not track_loudness:
                log.error("Failed to calculate track loudness")
                return False

            album_loudness = {}
            if album is not None:
                release_mbid_folder = os.path.dirname(cache_folder)
                album_data_file = os.path.join(release_mbid_folder, f"loudness_{config.setting['acousticbrainz_ng_replaygain_reference_loudness'] or -18}.json")

                if not os.path.exists(album_data_file):
                    album_track_files = []
                    
                    for track in album.tracks:
                        for file in track.files:
                            album_track_files.append(file.filename)
                    
                    if len(album_track_files) == 1:
                        album_loudness = {
                            "replaygain_album_gain": track_loudness.get("replaygain_track_gain"),
                            "replaygain_album_peak": track_loudness.get("replaygain_track_peak"),
                            "replaygain_album_range": track_loudness.get("replaygain_track_range")
                        }
                        if track_loudness.get("r128_track_gain") is not None:
                            album_loudness["r128_album_gain"] = track_loudness.get("r128_track_gain")
                    else:
                        album_loudness = self.calculate_album_loudness(album_track_files)
                        if not album_loudness:
                            log.error("Failed to calculate album loudness")

                    album_data = {
                        "track_count": len(album_track_files),
                        **album_loudness
                    }

                    with open(album_data_file, 'w', encoding='utf-8') as f:
                        json.dump(album_data, f, indent=2)
                else:
                    try:
                        with open(album_data_file, 'r', encoding='utf-8') as f:
                            album_data = json.load(f)
                            album_loudness = {
                                "replaygain_album_gain": album_data.get('replaygain_album_gain'),
                                "replaygain_album_peak": album_data.get('replaygain_album_peak'),
                                "replaygain_album_range": album_data.get('replaygain_album_range')
                            }
                            
                            if album_data.get('r128_album_gain') is not None:
                                album_loudness["r128_album_gain"] = album_data.get('r128_album_gain')
                    except (FileNotFoundError, json.JSONDecodeError) as e:
                        log.error(f"Error reading album data file: {e}")

            loudness_data = {
                **track_loudness,
                **album_loudness
            }
            
            with open(loudness_file, 'w', encoding='utf-8') as f:
                json.dump(loudness_data, f, indent=2)
            
            return True
        except Exception as e:
            log.error(f"Error calculating loudness: {e}")
            return False
    
    def parse_loudness(self, metadata: Dict, file: str) -> bool:
        try:
            output_path = self._generate_cache_folder(metadata, file)
            if not output_path:
                log.error("Failed to generate cache folder path")
                return False
        except Exception as e:
            log.error(f"Error generating cache folder: {e}")
            return False

        loudness_file = os.path.join(output_path, f"loudness_{config.setting['acousticbrainz_ng_replaygain_reference_loudness'] or -18}.json")
        if not os.path.exists(loudness_file):
            log.error(f"Loudness file not found: {loudness_file}")
            return False
        
        loudness_data = {}
        
        try:
            with open(loudness_file, 'r', encoding='utf-8') as f:
                loudness_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log.error(f"Error reading loudness file: {e}")
            return False

        try:
            is_opus = self._is_opus_file(file)

            replaygain_track_gain = loudness_data.get("replaygain_track_gain")
            if replaygain_track_gain is not None:
                metadata["replaygain_track_gain"] = f"{replaygain_track_gain} dB"
            
            replaygain_track_peak = loudness_data.get("replaygain_track_peak")
            if replaygain_track_peak is not None:
                metadata["replaygain_track_peak"] = replaygain_track_peak
            
            replaygain_track_range = loudness_data.get("replaygain_track_range")
            if replaygain_track_range is not None:
                metadata["replaygain_track_range"] = f"{replaygain_track_range} dB"
            
            replaygain_album_gain = loudness_data.get("replaygain_album_gain")
            if replaygain_album_gain is not None:
                metadata["replaygain_album_gain"] = f"{replaygain_album_gain} dB"
            
            replaygain_album_peak = loudness_data.get("replaygain_album_peak")
            if replaygain_album_peak is not None:
                metadata["replaygain_album_peak"] = replaygain_album_peak
            
            replaygain_album_range = loudness_data.get("replaygain_album_range")
            if replaygain_album_range is not None:
                metadata["replaygain_album_range"] = f"{replaygain_album_range} dB"
            
            replaygain_reference_loudness = loudness_data.get("replaygain_reference_loudness")
            if replaygain_reference_loudness is not None:
                metadata["replaygain_reference_loudness"] = f"{replaygain_reference_loudness} LUFS"
            
            if is_opus:
                r128_track_gain = loudness_data.get("r128_track_gain")
                if r128_track_gain is not None:
                    metadata["r128_track_gain"] = r128_track_gain
                
                r128_album_gain = loudness_data.get("r128_album_gain")
                if r128_album_gain is not None:
                    metadata["r128_album_gain"] = r128_album_gain

            return True
        except Exception as e:
            log.error(f"Error parsing loudness data: {e}")
            return False

acousticbrainz_ng = AcousticBrainzNG()

def analyze_track(track: Track, album: Optional[Album] = None) -> Dict:
    results = {
        'track': track,
        'album': album,
        'success': True,
        'errors': [],
        'files_processed': 0
    }

    semaphore = _get_analysis_semaphore()
    semaphore.acquire()
    
    try:
        for file in track.files:
            try:
                ar_result = acousticbrainz_ng.analyze_required(file.metadata, file.filename)
                pr_result = acousticbrainz_ng.parse_required(file.metadata, file.filename)

                if not ar_result or not pr_result:
                    error_msg = f"Failed to analyze required models for {file.filename}"
                    log.error(error_msg)
                    results['errors'].append(error_msg)
                    results['success'] = False
                    continue

                if config.setting["acousticbrainz_ng_analyze_optional"]:
                    ao_result = acousticbrainz_ng.analyze_optional(file.metadata, file.filename)
                    ap_result = acousticbrainz_ng.parse_optional(file.metadata, file.filename)
                    
                    if not ao_result or not ap_result:
                        error_msg = f"Failed to analyze optional models for {file.filename}"
                        log.error(error_msg)
                        results['errors'].append(error_msg)
                        
                if config.setting["acousticbrainz_ng_calculate_replaygain"]:
                    cl_result = acousticbrainz_ng.calculate_loudness(file.metadata, file.filename, album)
                    pl_result = acousticbrainz_ng.parse_loudness(file.metadata, file.filename)
                    
                    if not cl_result or not pl_result:
                        error_msg = f"Failed to calculate loudness for {file.filename}"
                        log.error(error_msg)
                        results['errors'].append(error_msg)

                if config.setting["acousticbrainz_ng_save_fingerprint"]:
                    sf_result = acousticbrainz_ng.save_fingerprint(file.metadata, file.filename, file)
                    if not sf_result:
                        error_msg = f"Failed to save fingerprint for {file.filename}"
                        log.error(error_msg)
                        results['errors'].append(error_msg)
                    else:
                        file.metadata_images_changed.emit()

                results['files_processed'] += 1

            except Exception as e:
                error_msg = f"Unexpected error analyzing {file.filename}: {str(e)}"
                log.error(error_msg)
                results['errors'].append(error_msg)
                results['success'] = False
    finally:
        semaphore.release()

    return results

class AcousticBrainzNGAction(BaseAction):
    NAME = f"[{PLUGIN_NAME}] Analyze"
    
    def __init__(self):
        super().__init__()
        self.num_tracks = 0
        self.current = 0

    def _format_progress(self):
        if self.num_tracks <= 1:
            return ""
        else:
            self.current += 1
            return f" ({self.current}/{self.num_tracks})"

    def _analysis_callback(self, result=None, error=None):
        progress = self._format_progress()
        
        if error is None and result:
            track = result['track']
            album = result['album']
            
            for file in track.files:
                file.update()
            track.update()
            if album:
                album.update()
            
            if result['success'] and not result['errors']:
                if album:
                    album_name = album.metadata.get('album', 'Unknown Album')
                    track_name = track.metadata.get('title', 'Unknown Track')
                    self.tagger.window.set_statusbar_message( # pyright: ignore[reportAttributeAccessIssue]
                        'Successfully analyzed "%s" from "%s"%s.', track_name, album_name, progress
                    )
                else:
                    track_name = track.metadata.get('title', 'Unknown Track')
                    self.tagger.window.set_statusbar_message( # pyright: ignore[reportAttributeAccessIssue]
                        'Successfully analyzed "%s"%s.', track_name, progress
                    )
            else:
                track_name = track.metadata.get('title', 'Unknown Track')
                if result['files_processed'] > 0:
                    self.tagger.window.set_statusbar_message( # pyright: ignore[reportAttributeAccessIssue]
                        'Partially analyzed "%s" with warnings%s.', track_name, progress
                    )
                else:
                    self.tagger.window.set_statusbar_message( # pyright: ignore[reportAttributeAccessIssue]
                        'Failed to analyze "%s"%s.', track_name, progress
                    )
        else:
            track_name = "Unknown Track"
            if result and result.get('track'):
                track_name = result['track'].metadata.get('title', 'Unknown Track')
            
            error_msg = str(error) if error else "Unknown error"
            log.error(f"Analysis failed for {track_name}: {error_msg}")
            self.tagger.window.set_statusbar_message( # pyright: ignore[reportAttributeAccessIssue]
                'Failed to analyze "%s"%s.', track_name, progress
            )

    def callback(self, objs):
        tracks_and_albums = [t for t in objs if isinstance(t, Track) or isinstance(t, Album)]
        
        if not tracks_and_albums:
            return
        
        total_files = 0
        tracks_to_process = []
        
        for item in tracks_and_albums:
            if isinstance(item, Track):
                total_files += len(item.files)
                tracks_to_process.append((item, None))
            elif isinstance(item, Album):
                for track in item.tracks:
                    total_files += len(track.files)
                    tracks_to_process.append((track, item))
        
        if not tracks_to_process:
            return
        
        self.num_tracks = len(tracks_to_process)
        self.current = 0
        
        if self.num_tracks == 1:
            track, album = tracks_to_process[0]
            track_name = track.metadata.get('title', 'Unknown Track')
            self.tagger.window.set_statusbar_message('Analyzing "%s" with %s...', track_name, PLUGIN_NAME) # pyright: ignore[reportAttributeAccessIssue]
        else:
            self.tagger.window.set_statusbar_message('Analyzing %i tracks with %s...', self.num_tracks, PLUGIN_NAME) # pyright: ignore[reportAttributeAccessIssue]
        
        log.debug(f"Analyzing {total_files} files from {self.num_tracks} tracks with {PLUGIN_NAME}")

        for track, album in tracks_to_process:
            thread.run_task(
                partial(analyze_track, track, album),
                self._analysis_callback
            )

class AcousticBrainzNGDeleteCacheAction(BaseAction):
    NAME = f"[{PLUGIN_NAME}] Delete cache"
    
    def __init__(self):
        super().__init__()
    
    def callback(self, objs):
        tracks_and_albums = [t for t in objs if isinstance(t, Track) or isinstance(t, Album)]
        
        if not tracks_and_albums:
            return
        
        total_files = 0
        tracks_to_process = []
        
        for item in tracks_and_albums:
            if isinstance(item, Track):
                total_files += len(item.files)
                tracks_to_process.append((item, None))
            elif isinstance(item, Album):
                for track in item.tracks:
                    total_files += len(track.files)
                    tracks_to_process.append((track, item))
        
        if not tracks_to_process:
            return

        num_tracks = len(tracks_to_process)
        current = 0
        
        if num_tracks == 1:
            track, album = tracks_to_process[0]
            track_name = track.metadata.get('title', 'Unknown Track')
            self.tagger.window.set_statusbar_message('Deleting %s cache for "%s"...', PLUGIN_NAME, track_name) # pyright: ignore[reportAttributeAccessIssue]
        else:
            self.tagger.window.set_statusbar_message('Deleting %s cache for %i tracks...', PLUGIN_NAME, num_tracks) # pyright: ignore[reportAttributeAccessIssue]
        
        log.debug(f"Deleting {PLUGIN_NAME} cache for {total_files} files from {num_tracks} tracks")
        
        for track, album in tracks_to_process:
            current += 1
            progress = f" ({current}/{num_tracks})" if num_tracks > 1 else ""
            for file in track.files:
                try:
                    cache_folder = acousticbrainz_ng._generate_cache_folder(file.metadata, file.filename)
                    if os.path.exists(cache_folder):
                        shutil.rmtree(cache_folder)
                        log.debug(f"Deleted cache folder: {cache_folder}")
                except Exception as e:
                    log.error(f"Error deleting cache for {file.filename}: {e}")
            track.update()
            if album:
                album.update()
            track_name = track.metadata.get('title', 'Unknown Track')
            self.tagger.window.set_statusbar_message('Deleted %s cache for "%s"%s.', PLUGIN_NAME, track_name, progress) # pyright: ignore[reportAttributeAccessIssue]

class AcousticBrainzNGOptionsPage(OptionsPage):
    NAME = "acousticbrainz_ng"
    TITLE = "AcousticBrainz-ng"
    PARENT = "plugins"
    
    options = CONFIG_OPTIONS
    
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
    
    def setup_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        
        options_group = QtWidgets.QGroupBox("Options", self)
        options_group.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum)
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
        musicnn_workers_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.musicnn_workers_input = QtWidgets.QSpinBox(self)
        self.musicnn_workers_input.setToolTip("Maximum number of concurrent MusicNN processes")
        self.musicnn_workers_input.setRange(1, max(len(REQUIRED_MODELS), len(OPTIONAL_MODELS)))
        self.musicnn_workers_input.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Preferred)
        
        musicnn_workers_layout.addWidget(musicnn_workers_label)
        musicnn_workers_layout.addStretch()
        musicnn_workers_layout.addWidget(self.musicnn_workers_input)
        
        concurrent_analyses_label = QtWidgets.QLabel("Max concurrent analyses:", self)
        concurrent_analyses_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.concurrent_analyses_input = QtWidgets.QSpinBox(self)
        self.concurrent_analyses_input.setToolTip("Maximum number of tracks analyzed simultaneously")
        self.concurrent_analyses_input.setRange(1, 8)
        self.concurrent_analyses_input.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Preferred)
        
        concurrent_analyses_layout.addWidget(concurrent_analyses_label)
        concurrent_analyses_layout.addStretch()
        concurrent_analyses_layout.addWidget(self.concurrent_analyses_input)
        
        rg_reference_label = QtWidgets.QLabel("ReplayGain reference loudness (LUFS):", self)
        rg_reference_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.rg_reference_input = QtWidgets.QSpinBox(self)
        self.rg_reference_input.setToolTip("ReplayGain reference loudness in LUFS")
        self.rg_reference_input.setRange(-30, -5)
        self.rg_reference_input.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Preferred)
        
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
        concurrent_processes_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.concurrent_processes_display = QtWidgets.QLabel("0", self)
        self.concurrent_processes_display.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
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
        paths_group.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum)
        paths_layout = QtWidgets.QVBoxLayout(paths_group)
        
        # Binaries path
        self.binaries_path_input = QtWidgets.QLineEdit(self)
        self.binaries_path_input.setPlaceholderText("Path to Essentia binaries")
        binaries_layout = self._create_path_input_layout(
            self.binaries_path_input,
            lambda: self._browse_folder(self.binaries_path_input),
            lambda: (self._check_binaries(show_success=True), None)[1]
        )
        
        # FFmpeg path
        self.ffmpeg_path_input = QtWidgets.QLineEdit(self)
        self.ffmpeg_path_input.setPlaceholderText("Path to FFmpeg")
        ffmpeg_layout = self._create_path_input_layout(
            self.ffmpeg_path_input,
            lambda: self._browse_file(self.ffmpeg_path_input),
            lambda: (self._check_binaries(show_success=True), None)[1]
        )

        # Models path
        self.models_path_input = QtWidgets.QLineEdit(self)
        self.models_path_input.setPlaceholderText("Path to MusicNN models")
        models_layout = self._create_path_input_layout(
            self.models_path_input,
            lambda: self._browse_folder(self.models_path_input),
            lambda: (self._check_models(show_success=True, check_optional=True), None)[1]
        )
        
        # Cache path
        self.cache_path_input = QtWidgets.QLineEdit(self)
        self.cache_path_input.setPlaceholderText("Path to cache directory")
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
        binaries_path = self.binaries_path_input.text()
        if not binaries_path or not os.path.exists(binaries_path):
            QtWidgets.QMessageBox.warning(self, "Binaries", "Invalid or empty binaries path.")
            return False
        
        ffmpeg_path = self.ffmpeg_path_input.text()
        if not ffmpeg_path or not os.path.exists(ffmpeg_path):
            QtWidgets.QMessageBox.warning(self, "Binaries", "Invalid or empty FFmpeg path.")
            return False
        
        missing_binaries = []
        for binary in REQUIRED_BINARIES:
            binary_path = os.path.join(binaries_path, binary)
            if os.name == 'nt':  # Windows
                binary_path += '.exe'
            if not os.path.exists(binary_path):
                missing_binaries.append(binary)
        
        try:
            result = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode != 0 or "ffmpeg version" not in result.stdout:
                missing_binaries.append("FFmpeg (invalid executable)")
                if result.returncode != 0:
                    log.error(f"FFmpeg version check failed with exit code {result.returncode}")
                    if result.stdout:
                        log.error(f"FFmpeg stdout: {result.stdout}")
                    if result.stderr:
                        log.error(f"FFmpeg stderr: {result.stderr}")
        except Exception as e:
            missing_binaries.append("FFmpeg (unable to execute)")
            log.error(f"Exception running FFmpeg version check: {e}")
        
        if missing_binaries:
            message = f"Missing binaries:\n" + "\n".join(f"• {binary}" for binary in missing_binaries)
            QtWidgets.QMessageBox.warning(self, "Binaries", message)
            return False
        else:
            if show_success:
                QtWidgets.QMessageBox.information(self, "Binaries", "All binaries found!")
            return True

    def _check_models(self, show_success=False, check_optional=True) -> bool:
        path = self.models_path_input.text()
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
            message = f"Note: Model JSON metadata required as well\n\nMissing required models:\n" + "\n".join(f"• {model}" for model in missing_required)
            QtWidgets.QMessageBox.warning(self, "Models", message)
            return False
        elif missing_optional and check_optional:
            message = f"Note: Model JSON metadata required as well\n\nMissing optional models:\n" + "\n".join(f"• {model}" for model in missing_optional)
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
        self.analyze_optional_checkbox.setChecked(config.setting["acousticbrainz_ng_analyze_optional"] or False)
        self.save_raw_checkbox.setChecked(config.setting["acousticbrainz_ng_save_raw"] or False)
        
        replaygain_setting = config.setting["acousticbrainz_ng_calculate_replaygain"]
        if replaygain_setting is None:
            self.calculate_replaygain_checkbox.setChecked(True)
        else:
            self.calculate_replaygain_checkbox.setChecked(replaygain_setting)
        
        self.rg_reference_input.setEnabled(self.calculate_replaygain_checkbox.isChecked())

        fingerprint_setting = config.setting["acousticbrainz_ng_save_fingerprint"]
        optional_setting = config.setting["acousticbrainz_ng_analyze_optional"] or False
        
        if fingerprint_setting is None:
            self.save_fingerprint_checkbox.setChecked(True if optional_setting else False)
        else:
            self.save_fingerprint_checkbox.setChecked(fingerprint_setting if optional_setting else False)
        
        self._update_fingerprint_state(optional_setting)

        self.musicnn_workers_input.setValue(config.setting["acousticbrainz_ng_max_musicnn_workers"] or 4)
        self.concurrent_analyses_input.setValue(config.setting["acousticbrainz_ng_max_concurrent_analyses"] or 2)
        self.rg_reference_input.setValue(config.setting["acousticbrainz_ng_replaygain_reference_loudness"] or -18)

        self.binaries_path_input.setText(config.setting["acousticbrainz_ng_binaries_path"])
        self.ffmpeg_path_input.setText(config.setting["acousticbrainz_ng_ffmpeg_path"])
        self.models_path_input.setText(config.setting["acousticbrainz_ng_models_path"])
        self.cache_path_input.setText(config.setting["acousticbrainz_ng_cache_path"])
    
    def save(self):
        self._check_binaries()
        self._check_models(show_success=False, check_optional=False)
        
        config.setting["acousticbrainz_ng_analyze_optional"] = self.analyze_optional_checkbox.isChecked()
        config.setting["acousticbrainz_ng_save_raw"] = self.save_raw_checkbox.isChecked()
        config.setting["acousticbrainz_ng_calculate_replaygain"] = self.calculate_replaygain_checkbox.isChecked()
        config.setting["acousticbrainz_ng_save_fingerprint"] = self.save_fingerprint_checkbox.isChecked()

        max_workers = max(1, min(self.musicnn_workers_input.value(), max(len(REQUIRED_MODELS), len(OPTIONAL_MODELS))))
        config.setting["acousticbrainz_ng_max_musicnn_workers"] = max_workers
        
        max_concurrent = max(1, min(self.concurrent_analyses_input.value(), 8))
        config.setting["acousticbrainz_ng_max_concurrent_analyses"] = max_concurrent
        
        rg_reference = max(-30, min(self.rg_reference_input.value(), -5))
        config.setting["acousticbrainz_ng_replaygain_reference_loudness"] = rg_reference

        config.setting["acousticbrainz_ng_binaries_path"] = self.binaries_path_input.text()
        config.setting["acousticbrainz_ng_ffmpeg_path"] = self.ffmpeg_path_input.text()
        config.setting["acousticbrainz_ng_models_path"] = self.models_path_input.text()
        config.setting["acousticbrainz_ng_cache_path"] = self.cache_path_input.text()

register_options_page(AcousticBrainzNGOptionsPage)
register_track_action(AcousticBrainzNGAction())
register_album_action(AcousticBrainzNGAction())
register_track_action(AcousticBrainzNGDeleteCacheAction())
register_album_action(AcousticBrainzNGDeleteCacheAction())