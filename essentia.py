import concurrent.futures
import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .audio import ProcessingFile
from .constants import (
    BUILD_INSTRUCTIONS,
    FINGERPRINT_VALUES,
    MUSICNN_CPU_BATCH_SIZE,
    MUSICNN_GPU_BATCH_SIZE,
    OPTIONAL_MODELS,
    REQUIRED_BINARIES,
    REQUIRED_MODELS,
)
from .fingerprint import fingerprint_png
from .gpu import tools_support_gpu
from .process import AnalysisCancelled, ProcessRunner
from .settings import Settings, exe

RHYTHM_FILE = "rhythm.yaml"
KEY_FILE = "key.yaml"


class Essentia:
    """Runs the Essentia based tools and parses their cached results"""

    def __init__(self, runner: ProcessRunner):
        self.runner = runner
        self.log = runner.log
        self._gpu_warned = False
        self._gpu_warning_lock = threading.Lock()

    @staticmethod
    def binary_path(settings: Settings, binary_name: str) -> str:
        return os.path.join(settings.binaries_path, exe(binary_name))

    def check_binaries(self, settings: Settings) -> bool:
        path = settings.binaries_path

        if not path or not os.path.exists(path):
            return False

        return all(os.path.exists(self.binary_path(settings, binary)) for binary in REQUIRED_BINARIES)

    @staticmethod
    def check_models(settings: Settings, models: List[Tuple[str, str]]) -> bool:
        path = settings.models_path

        if not path or not os.path.exists(path):
            return False

        return all(os.path.exists(os.path.join(path, f"{model[0]}.onnx")) for model in models)

    @staticmethod
    def pending_models(models: List[Tuple[str, str]], output_path: str) -> List[Tuple[str, str]]:
        return [model for model in models if not os.path.exists(os.path.join(output_path, f"{model[1]}.json"))]

    def has_required_outputs(self, output_path: str) -> bool:
        return (
            not self.pending_models(REQUIRED_MODELS, output_path)
            and os.path.exists(os.path.join(output_path, RHYTHM_FILE))
            and os.path.exists(os.path.join(output_path, KEY_FILE))
        )

    def _missing_binaries_error(self) -> None:
        self.log.error(f"Essentia binaries not found. {BUILD_INSTRUCTIONS}")

    def _run_musicnn_models(self, settings: Settings, models: List[Tuple[str, str]], file: str, output_path: str) -> bool:
        models_path = settings.models_path
        if not models_path:
            self.log.error("Models path not configured")
            return False

        musicnn_binary_path = self.binary_path(settings, "abng_musicnn")

        # (model name, model path, output path) of the models without results
        pending = []
        for model_name, output_file in models:
            model_path = os.path.join(models_path, f"{model_name}.onnx")
            if not os.path.exists(model_path):
                self.log.error(f"Model {model_name} not found at {model_path}")
                return False
            output_file_path = os.path.join(output_path, f"{output_file}.json")
            if not os.path.exists(output_file_path):
                pending.append((model_name, model_path, output_file_path))
        if not pending:
            return True

        # Each process decodes the audio once and runs its models one after
        # the other. On the GPU one process runs them all, on the CPU they are
        # split across processes that share the CPU cores
        gpu = settings.use_gpu and tools_support_gpu(settings.binaries_path)
        processes = 1 if gpu else min(settings.max_musicnn_workers, len(pending))
        threads = max(1, (os.cpu_count() or 1) // (processes * settings.max_concurrent_analyses))
        batch_size = MUSICNN_GPU_BATCH_SIZE if gpu else MUSICNN_CPU_BATCH_SIZE
        groups = [pending[i::processes] for i in range(processes)]

        def run_group(group: List[Tuple[str, str, str]]) -> None:
            arguments = [musicnn_binary_path, "--device", "gpu" if gpu else "cpu", "--batch-size", str(batch_size), "--threads", str(threads), file]
            for _model_name, model_path, output_file_path in group:
                arguments += [model_path, output_file_path]
            try:
                result = self.runner.run(arguments)
            except AnalysisCancelled:
                return
            except Exception as e:
                self.log.error(f"Error running MusicNN binary {musicnn_binary_path}: {e}")
                return

            if result.returncode != 0:
                names = ", ".join(model_name for model_name, _model_path, _output in group)
                self.runner.log_failure(f"MusicNN binary {musicnn_binary_path} failed for models {names} on file {file}", result)
            elif gpu and "GPU unavailable" in result.stderr:
                self._gpu_fallback(result.stderr)

        with concurrent.futures.ThreadPoolExecutor(max_workers=processes) as executor:
            list(executor.map(run_group, groups))
        self.runner.check_cancelled()

        return all(os.path.exists(output_file_path) for _model_name, _model_path, output_file_path in pending)

    def _gpu_fallback(self, stderr: str) -> None:
        # Only logged once, it would be the same for every file
        with self._gpu_warning_lock:
            if self._gpu_warned:
                return
            self._gpu_warned = True
        reason = next((line for line in stderr.splitlines() if "GPU unavailable" in line), stderr.strip())
        self.log.warning(f"Running MusicNN on the CPU instead of the GPU: {reason}")

    def analyze_required(self, settings: Settings, source: ProcessingFile, output_path: str) -> bool:
        if not self.check_binaries(settings):
            self._missing_binaries_error()
            return False

        if not self.check_models(settings, REQUIRED_MODELS):
            self.log.error("Required models not found")
            return False

        rhythm_yaml_path = os.path.join(output_path, RHYTHM_FILE)
        key_yaml_path = os.path.join(output_path, KEY_FILE)
        pending_models = self.pending_models(REQUIRED_MODELS, output_path)
        need_rhythm = not os.path.exists(rhythm_yaml_path)
        need_key = not os.path.exists(key_yaml_path)

        if not (pending_models or need_rhythm or need_key):
            return True

        processing_file = source.get()

        # Success of the rhythm and key tools, by binary name
        feature_results: Dict[str, bool] = {}
        def run_feature(binary: str, output_file: str) -> None:
            binary_path = self.binary_path(settings, binary)
            try:
                proc = self.runner.run([binary_path, processing_file, output_file])
            except AnalysisCancelled:
                feature_results[binary] = False
                return

            if proc.returncode != 0 or not os.path.exists(output_file):
                feature_results[binary] = False
                self.runner.log_failure(f"Binary {binary_path} failed on file {processing_file}", proc)
            else:
                feature_results[binary] = True

        threads = []
        if need_rhythm:
            threads.append(threading.Thread(target=run_feature, args=("abng_rhythm", rhythm_yaml_path)))
        if need_key:
            threads.append(threading.Thread(target=run_feature, args=("abng_key", key_yaml_path)))
        for feature_thread in threads:
            feature_thread.start()

        musicnn_success = True
        try:
            if pending_models:
                musicnn_success = self._run_musicnn_models(settings, pending_models, processing_file, output_path)
        finally:
            for feature_thread in threads:
                feature_thread.join()
        self.runner.check_cancelled()

        return musicnn_success and all(feature_results.values())

    def analyze_optional(self, settings: Settings, source: ProcessingFile, output_path: str) -> bool:
        if not self.check_binaries(settings):
            self._missing_binaries_error()
            return False

        if not self.check_models(settings, OPTIONAL_MODELS):
            self.log.error("Optional models not found")
            return False

        pending_models = self.pending_models(OPTIONAL_MODELS, output_path)
        if not pending_models:
            return True

        return self._run_musicnn_models(settings, pending_models, source.get(), output_path)

    def load_model_output(self, settings: Settings, model: str, output: str, output_path: str, repair: bool = True) -> Optional[Tuple[List[str], List[float]]]:
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
            # the next analysis recomputes it instead of failing forever. Not
            # done when only reading the cache, the file might still be written
            if repair:
                self.log.error(f"Error reading output file, removing it: {e}")
                try:
                    os.remove(output_file_path)
                except OSError:
                    pass
            else:
                self.log.warning(f"Error reading output file: {e}")
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

    @staticmethod
    def _format_class(class_name: str) -> str:
        return class_name.replace("non", "not").replace("_", " ").capitalize()

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

    def parse_required(self, settings: Settings, output_path: str, tags: Dict[str, Any], repair: bool = True) -> bool:
        if not self.check_models(settings, REQUIRED_MODELS):
            self.log.error("Required models not found")
            return False

        moods = []
        msd_tags = []

        for model, output in REQUIRED_MODELS:
            loaded = self.load_model_output(settings, model, output, output_path, repair)
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

        try:
            rhythm_data = self._load_yaml(os.path.join(output_path, RHYTHM_FILE), "rhythm")
            key_data = self._load_yaml(os.path.join(output_path, KEY_FILE), "key")
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

    def parse_optional(self, settings: Settings, output_path: str, tags: Dict[str, Any], repair: bool = True) -> bool:
        if not self.check_models(settings, OPTIONAL_MODELS):
            self.log.error("Optional models not found")
            return False

        for model, output in OPTIONAL_MODELS:
            loaded = self.load_model_output(settings, model, output, output_path, repair)
            if loaded is None:
                return False

            if settings.save_raw:
                tags.update(self._raw_tags(output, *loaded))

        return True

    def build_fingerprint(self, settings: Settings, output_path: str, repair: bool = True) -> Optional[Tuple[bytes, str]]:
        # Values are read from the cached model outputs in a fixed order, so the
        # fingerprint doesn't depend on "Save raw values" or on the tag order
        # of previously saved files
        fingerprint_data: List[float] = []

        for model, output in REQUIRED_MODELS + OPTIONAL_MODELS:
            loaded = self.load_model_output(settings, model, output, output_path, repair)
            if loaded is None:
                return None
            fingerprint_data.extend(float(value) for value in loaded[1])

        if len(fingerprint_data) != FINGERPRINT_VALUES:
            self.log.error(f"Fingerprint expected exactly {FINGERPRINT_VALUES} values, got {len(fingerprint_data)}")
            return None

        fingerprint_file = os.path.join(output_path, "fingerprint.png")

        try:
            png = fingerprint_png(fingerprint_data)
            with open(fingerprint_file, "wb") as f:
                f.write(png)
        except Exception as e:
            self.log.error(f"Failed to create fingerprint PNG: {e}")
            return None

        return png, fingerprint_file
