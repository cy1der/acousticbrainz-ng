import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from picard.plugin3.api import PluginApi
from PyQt6 import QtCore

from .audio import AudioTools, ProcessingFile
from .cache import album_key_entry, cache_folder
from .constants import ALBUM_LOUDNESS_FILE, OPTIONAL_MODELS
from .essentia import Essentia
from .loudness import AlbumJob, Loudness
from .process import AnalysisCancelled, ProcessRunner
from .settings import Settings, read_settings


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


class PluginSignals(QtCore.QObject):
    # Emitted with whether any analysis can be stopped
    analysis_running = QtCore.pyqtSignal(bool)


class AcousticBrainzNG:
    def __init__(self, api: PluginApi):
        self.api = api
        self.log = api.logger
        self.pool = QtCore.QThreadPool()
        # Files queued or being analyzed, only accessed on the main thread
        self.in_flight: set = set()
        # Analysis batches with tracks left to analyze, only accessed on the
        # main thread
        self.batches: set = set()
        self.signals = PluginSignals()
        self.runner = ProcessRunner(api.logger)
        self.audio = AudioTools(self.runner)
        self.essentia = Essentia(self.runner)
        self.loudness = Loudness(self.audio)

    def settings(self) -> Settings:
        return read_settings(self.api)

    def is_analyzing(self) -> bool:
        """Whether any analysis can still be stopped"""
        return any(not batch.stopped for batch in self.batches)

    def update_analysis_running(self) -> None:
        self.signals.analysis_running.emit(self.is_analyzing())

    def shutdown(self) -> None:
        self.runner.cancel()
        self.pool.clear()
        self.pool.waitForDone()

    def analyze_track(self, settings: Settings, jobs: List[FileJob], album: Optional[AlbumJob], should_continue: Callable[[], bool]) -> TrackResult:
        """Analyzes the files in order. should_continue is called before each
        file, once it returns False the remaining files are left out of the
        result"""
        results = TrackResult([])
        try:
            for job in jobs:
                if not should_continue():
                    break
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
            folder = cache_folder(settings, job.metadata, job.filename, self.audio)
        except AnalysisCancelled:
            raise
        except Exception as e:
            error(f"Error generating cache folder for {job.filename}: {e}")
            return

        with ProcessingFile(self.audio, settings, job.filename) as source:
            if not self.essentia.analyze_required(settings, source, folder) or not self.essentia.parse_required(settings, folder, result.tags):
                error(f"Failed to analyze required models for {job.filename}")
                return

            if settings.analyze_optional:
                if not self.essentia.analyze_optional(settings, source, folder) or not self.essentia.parse_optional(settings, folder, result.tags):
                    error(f"Failed to analyze optional models for {job.filename}")

        if settings.calculate_replaygain:
            entry = album_key_entry(job.filename, job.metadata)
            if not self.loudness.apply(settings, job.filename, folder, album, entry, result.tags):
                error(f"Failed to calculate loudness for {job.filename}")

        if settings.save_fingerprint:
            result.fingerprint = self.essentia.build_fingerprint(settings, folder)
            if result.fingerprint is None:
                error(f"Failed to save fingerprint for {job.filename}")

        result.processed = True

    def load_cached(self, settings: Settings, job: FileJob, album: Optional[AlbumJob]) -> Optional[FileResult]:
        """Returns the results of a previous analysis of the file without
        running any analysis, or None if there are none"""
        try:
            # Callers make sure the file has a recording MBID, the cache folder
            # of other files can only be found by hashing the audio
            folder = cache_folder(settings, job.metadata, job.filename, self.audio, create=False)
            if not os.path.isdir(folder) or not self.essentia.has_required_outputs(folder):
                return None

            result = FileResult(job.file)
            if not self.essentia.parse_required(settings, folder, result.tags, repair=False):
                return None

            optional_cached = not self.essentia.pending_models(OPTIONAL_MODELS, folder)
            if settings.analyze_optional and optional_cached:
                self.essentia.parse_optional(settings, folder, result.tags, repair=False)

            if settings.calculate_replaygain:
                entry = album_key_entry(job.filename, job.metadata)
                self.loudness.apply(settings, job.filename, folder, album, entry, result.tags, measure=False)

            if settings.save_fingerprint and optional_cached:
                result.fingerprint = self.essentia.build_fingerprint(settings, folder, repair=False)

            result.processed = True
            return result
        except Exception as e:
            self.log.error(f"Error loading cached results for {job.filename}: {e}")
            return None

    def delete_cache(self, settings: Settings, jobs: List[Tuple[FileJob, bool]]) -> None:
        for job, delete_album in jobs:
            try:
                folder = cache_folder(settings, job.metadata, job.filename, self.audio, create=False)
                if os.path.exists(folder):
                    shutil.rmtree(folder)
                    self.log.debug(f"Deleted cache folder: {folder}")
                album_file = os.path.join(os.path.dirname(folder), ALBUM_LOUDNESS_FILE)
                if delete_album and os.path.exists(album_file):
                    os.remove(album_file)
                    self.log.debug(f"Deleted album loudness cache: {album_file}")
            except Exception as e:
                self.log.error(f"Error deleting cache for {job.filename}: {e}")


_instance: Optional[AcousticBrainzNG] = None


def get_instance() -> Optional[AcousticBrainzNG]:
    return _instance


def set_instance(instance: Optional[AcousticBrainzNG]) -> None:
    global _instance
    _instance = instance
