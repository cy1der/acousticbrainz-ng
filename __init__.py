import os
import json
import subprocess
import hashlib
import threading
import concurrent.futures

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

    def _get_binary_paths(self) -> tuple[str, str]:
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

    def _run_musicnn_models(self, models: list[tuple[str, str]], musicnn_binary_path: str, file: str, output_path: str) -> None:
        models_path = config.setting["acousticbrainz_ng_models_path"]
        if not models_path:
            raise ValueError("Models path not configured")

        def run_musicnn_model(model_info):
            model_name, output_file = model_info
            try:
                model_path = os.path.join(models_path, f"{model_name}.pb")

                if not os.path.exists(model_path):
                    raise FileNotFoundError(f"Model {model_name} not found at {model_path}")

                output_file_path = os.path.join(output_path, f"{output_file}.json")
                
                if os.path.exists(output_file_path):
                    return
                
                subprocess.run(
                    [musicnn_binary_path, model_path, file, output_file_path],
                    capture_output=True,
                    text=True,
                    env=ENV
                )
            except FileNotFoundError as e:
                log.error(f"Model {model_name} not found: {e}")
            except Exception as e:
                log.error(f"Error processing model {model_name}: {e}")

        max_workers = config.setting["acousticbrainz_ng_max_musicnn_workers"] or 4
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(run_musicnn_model, model) for model in models]
            concurrent.futures.wait(futures)

    def analyze_required(self, metadata: dict, file: str) -> None:        
        if not self._check_binaries():
            log.error("Essentia binaries not found")
            return

        if not self._check_required_models():
            log.error("Required models not found")
            return
        
        try:
            musicnn_binary_path, gaia_binary_path = self._get_binary_paths()
        except (ValueError, FileNotFoundError) as e:
            log.error(str(e))
            return
    
        output_path = self._generate_cache_folder(metadata, file)
        if not output_path:
            raise ValueError("Failed to generate cache folder path")

        def run_gaia():
            if os.path.exists(os.path.join(output_path, "gaia.json")):
                return

            subprocess.run(
                [gaia_binary_path, file, os.path.join(output_path, "gaia.json")],
                capture_output=True,
                text=True,
                env=ENV
            )
        
        gaia_thread = threading.Thread(target=run_gaia)
        gaia_thread.start()

        self._run_musicnn_models(REQUIRED_MODELS, musicnn_binary_path, file, output_path)
        gaia_thread.join()

    def analyze_optional(self, metadata: dict, file: str) -> None:        
        if not self._check_binaries():
            log.error("Essentia binaries not found")
            return

        if not self._check_optional_models():
            log.error("Optional models not found")
            return
        
        try:
            musicnn_binary_path, _ = self._get_binary_paths()
        except (ValueError, FileNotFoundError) as e:
            log.error(str(e))
            return
    
        output_path = self._generate_cache_folder(metadata, file)
        if not output_path:
            raise ValueError("Failed to generate cache folder path")

        self._run_musicnn_models(OPTIONAL_MODELS, musicnn_binary_path, file, output_path)
    
    def parse_required(self, metadata: dict, file: str) -> None:
        if not self._check_required_models():
            raise ValueError("Required models not found")
        
        models_path = config.setting["acousticbrainz_ng_models_path"]
        if not models_path:
            raise ValueError("Models path not configured")

        output_path = self._generate_cache_folder(metadata, file)
        if not output_path:
            raise ValueError("Failed to generate cache folder path")
        
        moods = []
        tags = []
        
        for model, output in REQUIRED_MODELS:
            model_json_path = os.path.join(models_path, f"{model}.json")
            if not os.path.exists(model_json_path):
                log.error(f"Model JSON metadata not found: {model_json_path}")
                return
            
            output_file_path = os.path.join(output_path, f"{output}.json")
            if not os.path.exists(output_file_path):
                log.error(f"Output file not found: {output_file_path}")
                return
            
            output_data = {}
            model_metadata = {}

            try:
                with open(model_json_path, 'r', encoding='utf-8') as f:
                    model_metadata = json.load(f)
                
                with open(output_file_path, 'r', encoding='utf-8') as f:
                    output_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as e:
                log.error(f"Error reading model or output file: {e}")
                return

            if not output_data["predictions"] or not output_data["predictions"]["mean"]:
                log.error(f"No predictions found in output data for {model}")
                return
            
            if not model_metadata["classes"] or len(model_metadata["classes"]) != len(output_data["predictions"]["mean"]):
                log.error(f"No or invalid classes defined in model metadata for {model}")
                return

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
                return
        else:
            log.error(f"Gaia JSON file not found: {gaia_json_path}")
            return

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
            
            metadata["key"] = "o" if selected_key_data["scale"] == "off" else f"{selected_key_data["key"]}{"m" if selected_key_data["scale"] == "minor" else ""}"
            
            if config.setting["acousticbrainz_ng_save_raw"]:
                metadata["ab:lo:tonal:key_scale"] = selected_key_data["scale"]
                metadata["ab:lo:tonal:key_key"] = selected_key_data["key"]
    
    def parse_optional(self, metadata: dict, file: str) -> None:
        if not self._check_optional_models():
            raise ValueError("Optional models not found")
        
        models_path = config.setting["acousticbrainz_ng_models_path"]
        if not models_path:
            raise ValueError("Models path not configured")

        output_path = self._generate_cache_folder(metadata, file)
        if not output_path:
            raise ValueError("Failed to generate cache folder path")
        
        for model, output in OPTIONAL_MODELS:
            model_json_path = os.path.join(models_path, f"{model}.json")
            if not os.path.exists(model_json_path):
                log.error(f"Model JSON metadata not found: {model_json_path}")
                return
            
            output_file_path = os.path.join(output_path, f"{output}.json")
            if not os.path.exists(output_file_path):
                log.error(f"Output file not found: {output_file_path}")
                return
            
            output_data = {}
            model_metadata = {}

            try:
                with open(model_json_path, 'r', encoding='utf-8') as f:
                    model_metadata = json.load(f)
                
                with open(output_file_path, 'r', encoding='utf-8') as f:
                    output_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as e:
                log.error(f"Error reading model or output file: {e}")
                return

            if not output_data["predictions"] or not output_data["predictions"]["mean"]:
                log.error(f"No predictions found in output data for {model}")
                return
            
            if not model_metadata["classes"] or len(model_metadata["classes"]) != len(output_data["predictions"]["mean"]):
                log.error(f"No or invalid classes defined in model metadata for {model}")
                return
            
            if config.setting["acousticbrainz_ng_save_raw"]:
                for i in range(len(output_data["predictions"]["mean"])):
                    metadata[f"ab:hi:{output}:{model_metadata['classes'][i].replace('non', 'not').replace('_', ' ').lower()}"] = output_data["predictions"]["mean"][i]

    @staticmethod
    def _format_class(class_name: str) -> str:
        return class_name.replace("non", "not").replace("_", " ").capitalize()

    def _generate_cache_folder(self, metadata: dict, file_path: str) -> str:
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
                env=ENV
            )
            
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line.startswith('MD5:'):
                        return line.split('MD5:')[1].strip()
            
            log.error(f"Failed to calculate audio hash: {result.stderr}")
            
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

    def _check_models(self, models: list[tuple[str, str]]) -> bool:
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

acousticbrainz_ng = AcousticBrainzNG()

class AcousticBrainzNGAction(BaseAction):
    NAME = f"Analyze with {PLUGIN_NAME}"
    
    def _process_track(self, track: Track) -> None:
        for file in track.files:
            acousticbrainz_ng.analyze_required(file.metadata, file.filename)
            acousticbrainz_ng.parse_required(file.metadata, file.filename)
            # TODO: Implement track replaygain
            
            if config.setting["acousticbrainz_ng_analyze_optional"]:
                acousticbrainz_ng.analyze_optional(file.metadata, file.filename)
                acousticbrainz_ng.parse_optional(file.metadata, file.filename)
    
    def callback(self, objs):
        for item in (t for t in objs if isinstance(t, Track) or isinstance(t, Album)):
            if isinstance(item, Track):
                self._process_track(item)
            elif isinstance(item, Album):
                for track in item.tracks:
                    self._process_track(track)

                # TODO: Implement album replaygain

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
        
        self.autorun_checkbox = QtWidgets.QCheckBox("Autorun analysis", self)
        self.autorun_checkbox.setToolTip("Automatically run analysis on new tracks")

        self.analyze_optional_checkbox = QtWidgets.QCheckBox("Analyze optional MusicNN models", self)
        self.analyze_optional_checkbox.setToolTip("Include optional MusicNN models in the analysis, currently unused unless raw values is enabled")

        self.save_raw_checkbox = QtWidgets.QCheckBox("Save raw values", self)
        self.save_raw_checkbox.setToolTip("Save raw MusicNN numbers in the metadata")
        
        musicnn_workers_layout = QtWidgets.QHBoxLayout()
        
        musicnn_workers_label = QtWidgets.QLabel("Max MusicNN workers:", self)
        musicnn_workers_label.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Preferred)
        self.musicnn_workers_input = QtWidgets.QSpinBox(self)
        self.musicnn_workers_input.setToolTip("Maximum number of concurrent MusicNN workers")
        self.musicnn_workers_input.setRange(1, max(len(REQUIRED_MODELS), len(OPTIONAL_MODELS)))
        self.musicnn_workers_input.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Preferred)
        
        musicnn_workers_layout.addWidget(musicnn_workers_label)
        musicnn_workers_layout.addStretch()
        musicnn_workers_layout.addWidget(self.musicnn_workers_input)
        
        options_layout.addWidget(self.autorun_checkbox)
        options_layout.addWidget(self.analyze_optional_checkbox)
        options_layout.addWidget(self.save_raw_checkbox)
        options_layout.addLayout(musicnn_workers_layout)

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

        paths_layout.addWidget(QtWidgets.QLabel("Binaries", self))
        paths_layout.addLayout(binaries_layout)
        paths_layout.addWidget(QtWidgets.QLabel("Models", self))
        paths_layout.addLayout(models_layout)
        paths_layout.addWidget(QtWidgets.QLabel("Cache", self))
        paths_layout.addLayout(cache_layout)
        
        layout.addWidget(paths_group)
        
        layout.addStretch()

    def _check_binaries(self, show_success=False) -> bool:
        path = self.binaries_path_input.text()
        if not path or not os.path.exists(path):
            QtWidgets.QMessageBox.warning(self, "Binaries", "Invalid or empty path.")
            return False
        
        missing_binaries = []
        for binary in REQUIRED_BINARIES:
            binary_path = os.path.join(path, binary)
            if os.name == 'nt':  # Windows
                binary_path += '.exe'
            if not os.path.exists(binary_path):
                missing_binaries.append(binary)
        
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

    def load(self):
        self.autorun_checkbox.setChecked(config.setting["acousticbrainz_ng_autorun"] or False)
        self.analyze_optional_checkbox.setChecked(config.setting["acousticbrainz_ng_analyze_optional"] or False)
        self.save_raw_checkbox.setChecked(config.setting["acousticbrainz_ng_save_raw"] or False)
        
        self.musicnn_workers_input.setValue(config.setting["acousticbrainz_ng_max_musicnn_workers"] or 4)

        self.binaries_path_input.setText(config.setting["acousticbrainz_ng_binaries_path"])
        self.models_path_input.setText(config.setting["acousticbrainz_ng_models_path"])
        self.cache_path_input.setText(config.setting["acousticbrainz_ng_cache_path"])
    
    def save(self):
        self._check_binaries()
        self._check_models(show_success=False, check_optional=False)
        
        config.setting["acousticbrainz_ng_autorun"] = self.autorun_checkbox.isChecked()
        config.setting["acousticbrainz_ng_analyze_optional"] = self.analyze_optional_checkbox.isChecked()
        config.setting["acousticbrainz_ng_save_raw"] = self.save_raw_checkbox.isChecked()

        max_workers = max(1, min(self.musicnn_workers_input.value(), max(len(REQUIRED_MODELS), len(OPTIONAL_MODELS))))
        config.setting["acousticbrainz_ng_max_musicnn_workers"] = max_workers

        config.setting["acousticbrainz_ng_binaries_path"] = self.binaries_path_input.text()
        config.setting["acousticbrainz_ng_models_path"] = self.models_path_input.text()
        config.setting["acousticbrainz_ng_cache_path"] = self.cache_path_input.text()

register_options_page(AcousticBrainzNGOptionsPage)
register_track_action(AcousticBrainzNGAction())
register_album_action(AcousticBrainzNGAction())