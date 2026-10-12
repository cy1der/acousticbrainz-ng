import hashlib
import os
import re
import shutil
import tempfile
from typing import Optional, Tuple

from .process import AnalysisCancelled, ProcessRunner
from .settings import Settings, exe


class AudioTools:
    """Audio file helpers built on FFmpeg"""

    def __init__(self, runner: ProcessRunner):
        self.runner = runner
        self.log = runner.log

    @staticmethod
    def is_opus(file_path: str) -> bool:
        return file_path.lower().endswith('.opus')

    def get_audio_hash(self, settings: Settings, file_path: str) -> str:
        # MD5 of the undecoded audio packets, ignoring metadata and cover art.
        # This is the same value Essentia's streaming_md5 computed, so existing
        # caches stay valid, but it also works for more than 2 channels
        try:
            if not settings.ffmpeg_path:
                raise ValueError("FFmpeg path not configured")

            result = self.runner.run([settings.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-i", file_path, "-map", "0:a:0", "-c", "copy", "-f", "md5", "-"])

            if result.returncode == 0:
                match = re.search(r'MD5=([0-9a-f]{32})', result.stdout)
                if match:
                    return match.group(1)
            else:
                self.runner.log_failure(f"FFmpeg audio hash failed on file {file_path}", result)

            self.log.error(f"Failed to calculate audio hash for file {file_path}: MD5 not found in output")

        except AnalysisCancelled:
            raise
        except Exception as e:
            self.log.error(f"Error calculating audio hash: {e}")

        return f"fallback_{hashlib.md5(file_path.encode('utf-8')).hexdigest()}"

    def get_channel_count(self, settings: Settings, file_path: str) -> int:
        try:
            ffmpeg_path = settings.ffmpeg_path
            if not ffmpeg_path:
                self.log.warning("FFmpeg path not configured, assuming stereo")
                return 2

            ffprobe_path = os.path.join(os.path.dirname(ffmpeg_path), exe("ffprobe"))

            if not os.path.exists(ffprobe_path):
                probe_proc = self.runner.run([ffmpeg_path, "-hide_banner", "-nostdin", "-i", file_path])

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
                probe_proc = self.runner.run(
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

    def create_stereo_downmix(self, settings: Settings, file_path: str) -> Optional[str]:
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

            downmix_proc = self.runner.run([ffmpeg_path, "-hide_banner", "-nostdin", "-i", file_path, "-ac", "2", "-y", temp_file])

            if downmix_proc.returncode != 0:
                self.runner.log_failure(f"FFmpeg downmix failed for {file_path}", downmix_proc)
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

    def cleanup_temp_file(self, temp_file_path: str) -> None:
        try:
            if temp_file_path and os.path.exists(temp_file_path):
                temp_dir = os.path.dirname(temp_file_path)
                shutil.rmtree(temp_dir)
                self.log.debug(f"Cleaned up temporary directory: {temp_dir}")
        except Exception as e:
            self.log.warning(f"Failed to clean up temporary file {temp_file_path}: {e}")

    def get_processing_file(self, settings: Settings, file_path: str) -> Tuple[str, Optional[str]]:
        channels = self.get_channel_count(settings, file_path)

        if channels > 2:
            self.log.info(f"File {file_path} has {channels} channels, creating stereo downmix for processing")
            temp_file = self.create_stereo_downmix(settings, file_path)
            if temp_file:
                return (temp_file, temp_file)
            else:
                self.log.warning(f"Failed to create stereo downmix for {file_path}, attempting to process original")
                return (file_path, None)

        return (file_path, None)


class ProcessingFile:
    """Lazily provides a stereo version of an audio file for Essentia, only
    probing and downmixing when an analysis actually needs to run"""

    def __init__(self, audio: AudioTools, settings: Settings, file_path: str):
        self._audio = audio
        self._settings = settings
        self._file_path = file_path
        self._processing_file: Optional[str] = None
        self._temp_file: Optional[str] = None

    def get(self) -> str:
        if self._processing_file is None:
            self._processing_file, self._temp_file = self._audio.get_processing_file(self._settings, self._file_path)
        return self._processing_file

    def __enter__(self) -> "ProcessingFile":
        return self

    def __exit__(self, *exc) -> None:
        if self._temp_file:
            self._audio.cleanup_temp_file(self._temp_file)
