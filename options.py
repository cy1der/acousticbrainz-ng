import os
import subprocess

from picard.plugin3.api import OptionsPage
from PyQt6 import QtWidgets, QtCore

from .constants import (
    BUILD_INSTRUCTIONS,
    OPTIONAL_MODELS,
    PLUGIN_NAME,
    PLUGIN_OPTIONS,
    REQUIRED_BINARIES,
    REQUIRED_MODELS,
)
from .settings import default_path, exe, resolve_path


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
        default = default_path(self.api, name)
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

        self.auto_apply_cached_checkbox = QtWidgets.QCheckBox("Automatically apply cached results to matched files", self)
        self.auto_apply_cached_checkbox.setToolTip("Apply the results of previous analyses when files are matched to tracks, without analyzing them again")

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
        options_layout.addWidget(self.auto_apply_cached_checkbox)
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
        binaries_path = resolve_path(self.api, "binaries_path", self.binaries_path_input.text())
        if not binaries_path or not os.path.exists(binaries_path):
            QtWidgets.QMessageBox.warning(self, "Binaries", f"Invalid or empty binaries path.\n\n{BUILD_INSTRUCTIONS}.")
            return False

        ffmpeg_path = resolve_path(self.api, "ffmpeg_path", self.ffmpeg_path_input.text())
        if not ffmpeg_path or not os.path.exists(ffmpeg_path):
            QtWidgets.QMessageBox.warning(self, "Binaries", "Invalid or empty FFmpeg path.")
            return False

        missing_binaries = []
        for binary in REQUIRED_BINARIES:
            if not os.path.exists(os.path.join(binaries_path, exe(binary))):
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
            message = "Missing binaries:\n" + "\n".join(f"• {binary}" for binary in missing_binaries) + f"\n\n{BUILD_INSTRUCTIONS}."
            QtWidgets.QMessageBox.warning(self, "Binaries", message)
            return False
        else:
            if show_success:
                QtWidgets.QMessageBox.information(self, "Binaries", "All binaries found!")
            return True

    def _check_models(self, show_success=False, check_optional=True) -> bool:
        path = resolve_path(self.api, "models_path", self.models_path_input.text())
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
        self.auto_apply_cached_checkbox.setChecked(cfg["auto_apply_cached"])
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
        cfg["auto_apply_cached"] = self.auto_apply_cached_checkbox.isChecked()
        cfg["save_fingerprint"] = self.save_fingerprint_checkbox.isChecked()

        cfg["max_musicnn_workers"] = self.musicnn_workers_input.value()
        cfg["max_concurrent_analyses"] = self.concurrent_analyses_input.value()
        cfg["replaygain_reference_loudness"] = self.rg_reference_input.value()

        cfg["binaries_path"] = self.binaries_path_input.text().strip()
        cfg["ffmpeg_path"] = self.ffmpeg_path_input.text().strip()
        cfg["models_path"] = self.models_path_input.text().strip()
        cfg["cache_path"] = self.cache_path_input.text().strip()
