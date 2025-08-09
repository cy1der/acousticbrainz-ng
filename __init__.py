import os
import json
import subprocess
import hashlib
import threading
import concurrent.futures
import tempfile
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
from PyQt5 import QtWidgets

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

    def _get_binary_paths(self) -> Tuple[str, str]:
        binaries_path = config.setting["acousticbrainz_ng_binaries_path"]
        if not binaries_path:
            raise ValueError("Binaries path not configured")
            
        musicnn_binary_path = self._get_binary_path("streaming_musicnn_predict", binaries_path)
        gaia_binary_path = self._get_binary_path("streaming_extractor_music", binaries_path)
        
        if not os.path.exists(musicnn_binary_path):
            raise FileNotFoundError(f"Binary {musicnn_binary_path} not found")
        if not os.path.exists(gaia_binary_path):
            raise FileNotFoundError(f"Binary {gaia_binary_path} not found")
            
        return musicnn_binary_path, gaia_binary_path

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
            musicnn_binary_path, gaia_binary_path = self._get_binary_paths()
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

        gaia_success = True
        def run_gaia():
            nonlocal gaia_success
            if os.path.exists(os.path.join(output_path, "gaia.json")):
                return

            result = subprocess.run(
                [gaia_binary_path, file, os.path.join(output_path, "gaia.json")],
                capture_output=True,
                text=True,
                env=ENV,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if result.returncode != 0:
                gaia_success = False
                log.error(f"Gaia binary {gaia_binary_path} failed on file {file} with exit code {result.returncode}")
                if result.stdout:
                    log.error(f"Gaia stdout: {result.stdout}")
                if result.stderr:
                    log.error(f"Gaia stderr: {result.stderr}")
        
        gaia_thread = threading.Thread(target=run_gaia)
        gaia_thread.start()

        musicnn_success = self._run_musicnn_models(REQUIRED_MODELS, musicnn_binary_path, file, output_path)
        gaia_thread.join()
        
        return gaia_success and musicnn_success

    def analyze_optional(self, metadata: Dict, file: str) -> bool:        
        if not self._check_binaries():
            log.error("Essentia binaries not found")
            return False

        if not self._check_optional_models():
            log.error("Optional models not found")
            return False
        
        try:
            musicnn_binary_path, _ = self._get_binary_paths()
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

        return self._run_musicnn_models(OPTIONAL_MODELS, musicnn_binary_path, file, output_path)
    
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
        
        gaia_data = {}
        gaia_json_path = os.path.join(output_path, "gaia.json")

        if os.path.exists(gaia_json_path):
            try:
                with open(gaia_json_path, 'r', encoding='utf-8') as f:
                    gaia_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as e:
                log.error(f"Error reading Gaia JSON file: {e}")
                return False
        else:
            log.error(f"Gaia JSON file not found: {gaia_json_path}")
            return False

        try:
            metadata["bpm"] = int(round(gaia_data["rhythm"]["bpm"]))
            
            if config.setting["acousticbrainz_ng_save_raw"]:
                metadata["ab:lo:tonal:chords_changes_rate"] = gaia_data["tonal"]["chords_changes_rate"]
                metadata["ab:lo:tonal:chords_key"] = gaia_data["tonal"]["chords_key"]
                metadata["ab:lo:tonal:chords_scale"] = gaia_data["tonal"]["chords_scale"]
            
            highestStrength = -1
            selectedAlgorithm = None
            
            for algorithm in GAIA_KEY_ALGORITHMS:
                key_data = gaia_data["tonal"][f"key_{algorithm}"]

                if key_data["strength"] > highestStrength:
                    highestStrength = key_data["strength"]
                    selectedAlgorithm = algorithm
            
            if selectedAlgorithm:
                selected_key_data = gaia_data["tonal"][f"key_{selectedAlgorithm}"]

                metadata["key"] = "o" if selected_key_data["scale"] == "off" else f"{selected_key_data['key']}{'m' if selected_key_data['scale'] == 'minor' else ''}"

                if config.setting["acousticbrainz_ng_save_raw"]:
                    metadata["ab:lo:tonal:key_scale"] = selected_key_data["scale"]
                    metadata["ab:lo:tonal:key_key"] = selected_key_data["key"]
            
            return True
        except Exception as e:
            log.error(f"Error processing gaia data: {e}")
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
    
    def calculate_track_loudness(self, file_path: str) -> Dict:
        try:
            ffmpeg_path = config.setting["acousticbrainz_ng_ffmpeg_path"]
            if not ffmpeg_path:
                raise ValueError("FFmpeg path not configured")
            
            replaygain_lufs_result = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-i", file_path, "-af", f"loudnorm=I={config.setting['acousticbrainz_ng_replaygain_reference_loudness']}:print_format=json", "-f", "null", "-"],
                capture_output=True,
                text=True,
                env=ENV,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if replaygain_lufs_result.returncode != 0:
                log.error(f"FFmpeg failed for ReplayGain LUFS calculation on file {file_path} with exit code {replaygain_lufs_result.returncode}")
                if replaygain_lufs_result.stdout:
                    log.error(f"FFmpeg stdout: {replaygain_lufs_result.stdout}")
                if replaygain_lufs_result.stderr:
                    log.error(f"FFmpeg stderr: {replaygain_lufs_result.stderr}")
                return {}
            
            replaygain_gain = None
            replaygain_peak = None
            replaygain_range = None
            
            try:
                json_start = replaygain_lufs_result.stderr.find('{')
                if json_start != -1:
                    json_str = replaygain_lufs_result.stderr[json_start:]
                    json_end = json_str.find('}') + 1
                    if json_end > 0:
                        loudnorm_data = json.loads(json_str[:json_end])
                        input_i = loudnorm_data.get('input_i')
                        input_tp = loudnorm_data.get('input_tp')
                        input_lra = loudnorm_data.get('input_lra')
                        
                        if input_i and input_i != "-inf":
                            replaygain_gain = f"{(config.setting['acousticbrainz_ng_replaygain_reference_loudness'] or -18) - float(input_i):.2f}"

                        if input_tp and input_tp != "-inf":
                            replaygain_peak = f"{10 ** (float(input_tp) / 20):.6f}"
                        
                        if input_lra and input_lra != "-inf":
                            replaygain_range = f"{float(input_lra):.2f}"
                            
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
            
            result: Dict = {
                "replaygain_track_gain": replaygain_gain,
                "replaygain_track_peak": replaygain_peak,
                "replaygain_track_range": replaygain_range,
                "replaygain_reference_loudness": f"{(config.setting['acousticbrainz_ng_replaygain_reference_loudness'] or -18):.2f}"
            }
            
            r128_result = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-i", file_path, "-af", "loudnorm=I=-23:print_format=json", "-f", "null", "-"],
                capture_output=True,
                text=True,
                env=ENV,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if r128_result.returncode != 0:
                log.error(f"FFmpeg failed for R128 calculation on file {file_path} with exit code {r128_result.returncode}")
                if r128_result.stdout:
                    log.error(f"FFmpeg stdout: {r128_result.stdout}")
                if r128_result.stderr:
                    log.error(f"FFmpeg stderr: {r128_result.stderr}")
                return result
            
            r128_track_gain = None
            
            try:
                json_start = r128_result.stderr.find('{')
                if json_start != -1:
                    json_str = r128_result.stderr[json_start:]
                    json_end = json_str.find('}') + 1
                    if json_end > 0:
                        r128_data = json.loads(json_str[:json_end])
                        r128_input_i = r128_data.get('input_i')
                        
                        if r128_input_i and r128_input_i != "-inf":
                            r128_gain_db = -23 - float(r128_input_i)
                            r128_track_gain = int(round(r128_gain_db * 256))
                            
                            if r128_track_gain < -32768:
                                r128_track_gain = -32768
                            elif r128_track_gain > 32767:
                                r128_track_gain = 32767
                                
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
            
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
                    concat_file.write(f"file '{audio_file}'\n")
                concat_file_path = concat_file.name
            
            try:
                album_replaygain_result = subprocess.run(
                    [ffmpeg_path, "-hide_banner", "-f", "concat", "-safe", "0", "-i", concat_file_path, 
                     "-vn", "-af", f"loudnorm=I={config.setting['acousticbrainz_ng_replaygain_reference_loudness']}:print_format=json", "-f", "null", "-"],
                    capture_output=True,
                    text=True,
                    env=ENV,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                
                if album_replaygain_result.returncode != 0:
                    log.error(f"FFmpeg failed for album ReplayGain calculation on {len(album_track_files)} files with exit code {album_replaygain_result.returncode}")
                    log.error(f"Album files: {', '.join(album_track_files)}")
                    if album_replaygain_result.stdout:
                        log.error(f"FFmpeg stdout: {album_replaygain_result.stdout}")
                    if album_replaygain_result.stderr:
                        log.error(f"FFmpeg stderr: {album_replaygain_result.stderr}")
                    return {}
                
                album_gain = None
                album_peak = None
                album_range = None
                
                try:
                    json_start = album_replaygain_result.stderr.find('{')
                    if json_start != -1:
                        json_str = album_replaygain_result.stderr[json_start:]
                        json_end = json_str.find('}') + 1
                        if json_end > 0:
                            loudnorm_data = json.loads(json_str[:json_end])
                            input_i = loudnorm_data.get('input_i')
                            input_tp = loudnorm_data.get('input_tp')
                            input_lra = loudnorm_data.get('input_lra')
                            
                            if input_i and input_i != "-inf":
                                album_gain = f"{(config.setting['acousticbrainz_ng_replaygain_reference_loudness'] or -18) - float(input_i):.2f}"

                            if input_tp and input_tp != "-inf":
                                album_peak = f"{10 ** (float(input_tp) / 20):.6f}"
                            
                            if input_lra and input_lra != "-inf":
                                album_range = f"{float(input_lra):.2f}"
                                
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
                
                result: Dict = {
                    "replaygain_album_gain": album_gain,
                    "replaygain_album_peak": album_peak,
                    "replaygain_album_range": album_range
                }
                
                album_r128_result = subprocess.run(
                    [ffmpeg_path, "-hide_banner", "-f", "concat", "-safe", "0", "-i", concat_file_path,
                     "-vn", "-af", "loudnorm=I=-23:print_format=json", "-f", "null", "-"],
                    capture_output=True,
                    text=True,
                    env=ENV,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                
                if album_r128_result.returncode != 0:
                    log.error(f"FFmpeg failed for album R128 calculation on {len(album_track_files)} files with exit code {album_r128_result.returncode}")
                    log.error(f"Album files: {', '.join(album_track_files)}")
                    if album_r128_result.stdout:
                        log.error(f"FFmpeg stdout: {album_r128_result.stdout}")
                    if album_r128_result.stderr:
                        log.error(f"FFmpeg stderr: {album_r128_result.stderr}")
                    return result
                
                r128_album_gain = None
                
                try:
                    json_start = album_r128_result.stderr.find('{')
                    if json_start != -1:
                        json_str = album_r128_result.stderr[json_start:]
                        json_end = json_str.find('}') + 1
                        if json_end > 0:
                            r128_data = json.loads(json_str[:json_end])
                            r128_input_i = r128_data.get('input_i')
                            
                            if r128_input_i and r128_input_i != "-inf":
                                r128_gain_db = -23 - float(r128_input_i)
                                r128_album_gain = int(round(r128_gain_db * 256))
                                
                                if r128_album_gain < -32768:
                                    r128_album_gain = -32768
                                elif r128_album_gain > 32767:
                                    r128_album_gain = 32767
                                    
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

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
            
            # Check if track loudness calculation failed
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
                        # Check if album loudness calculation failed
                        if not album_loudness:
                            log.error("Failed to calculate album loudness")
                            # Continue with track-only data

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
                        # Continue without album loudness data

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

class AcousticBrainzNGAction(BaseAction):
    NAME = f"Analyze with {PLUGIN_NAME}"
    
    def _process_track(self, track: Track, album: Optional[Album] = None) -> None:
        window = self.tagger.window

        for file in track.files:
            ar_result = acousticbrainz_ng.analyze_required(file.metadata, file.filename)
            pr_result = acousticbrainz_ng.parse_required(file.metadata, file.filename)

            if not ar_result or not pr_result:
                log.error(f"Failed to analyze required models for {file.filename}")
                window.set_statusbar_message("Failed to analyze required models for %s", file.filename)
                continue
            else:
                window.set_statusbar_message("Analyzed required models for %s", file.filename)

            if config.setting["acousticbrainz_ng_analyze_optional"]:
                ao_result = acousticbrainz_ng.analyze_optional(file.metadata, file.filename)
                ap_result = acousticbrainz_ng.parse_optional(file.metadata, file.filename)
                
                if not ao_result or not ap_result:
                    log.error(f"Failed to analyze optional models for {file.filename}")
                    window.set_statusbar_message("Failed to analyze optional models for %s", file.filename)
                else:
                    window.set_statusbar_message("Analyzed optional models for %s", file.filename)

            if config.setting["acousticbrainz_ng_calculate_replaygain"]:
                cl_result = acousticbrainz_ng.calculate_loudness(file.metadata, file.filename, album)
                pl_result = acousticbrainz_ng.parse_loudness(file.metadata, file.filename)
                
                if not cl_result or not pl_result:
                    log.error(f"Failed to calculate loudness for {file.filename}")
                    window.set_statusbar_message("Failed to calculate loudness for %s", file.filename)
                else:
                    window.set_statusbar_message("Analyzed loudness for %s", file.filename)

            window.set_statusbar_message("Analyzed %s with %s", file.filename, PLUGIN_NAME)

    def callback(self, objs):
        for item in (t for t in objs if isinstance(t, Track) or isinstance(t, Album)):
            if isinstance(item, Track):
                self._process_track(item)
            elif isinstance(item, Album):
                for track in item.tracks:
                    self._process_track(track, item)

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
        self.analyze_optional_checkbox.setToolTip("Include optional MusicNN models in the analysis, currently unused unless raw values is enabled")

        self.save_raw_checkbox = QtWidgets.QCheckBox("Save raw values", self)
        self.save_raw_checkbox.setToolTip("Save raw MusicNN numbers in the metadata")
        
        self.calculate_replaygain_checkbox = QtWidgets.QCheckBox("Calculate ReplayGain", self)
        self.calculate_replaygain_checkbox.setToolTip("Calculate ReplayGain values for the track and album")
        
        musicnn_workers_layout = QtWidgets.QHBoxLayout()
        rg_reference_layout = QtWidgets.QHBoxLayout()
        
        musicnn_workers_label = QtWidgets.QLabel("Max MusicNN workers:", self)
        musicnn_workers_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.musicnn_workers_input = QtWidgets.QSpinBox(self)
        self.musicnn_workers_input.setToolTip("Maximum number of concurrent MusicNN workers")
        self.musicnn_workers_input.setRange(1, max(len(REQUIRED_MODELS), len(OPTIONAL_MODELS)))
        self.musicnn_workers_input.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Preferred)
        
        musicnn_workers_layout.addWidget(musicnn_workers_label)
        musicnn_workers_layout.addStretch()
        musicnn_workers_layout.addWidget(self.musicnn_workers_input)
        
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
        options_layout.addWidget(self.calculate_replaygain_checkbox)
        options_layout.addLayout(musicnn_workers_layout)
        options_layout.addLayout(rg_reference_layout)

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
        
        self.musicnn_workers_input.setValue(config.setting["acousticbrainz_ng_max_musicnn_workers"] or 4)
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

        max_workers = max(1, min(self.musicnn_workers_input.value(), max(len(REQUIRED_MODELS), len(OPTIONAL_MODELS))))
        config.setting["acousticbrainz_ng_max_musicnn_workers"] = max_workers
        
        rg_reference = max(-30, min(self.rg_reference_input.value(), -5))
        config.setting["acousticbrainz_ng_replaygain_reference_loudness"] = rg_reference

        config.setting["acousticbrainz_ng_binaries_path"] = self.binaries_path_input.text()
        config.setting["acousticbrainz_ng_ffmpeg_path"] = self.ffmpeg_path_input.text()
        config.setting["acousticbrainz_ng_models_path"] = self.models_path_input.text()
        config.setting["acousticbrainz_ng_cache_path"] = self.cache_path_input.text()

register_options_page(AcousticBrainzNGOptionsPage)
register_track_action(AcousticBrainzNGAction())
register_album_action(AcousticBrainzNGAction())