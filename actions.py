import os
from dataclasses import dataclass
from functools import partial
from typing import Any, Dict, List, Optional

from picard.plugin3.api import (
    Album,
    BaseAction,
    Cluster,
    CoverArtImage,
    File,
    PluginApi,
    Track,
)
from picard.util import thread
from PyQt6 import QtCore

from .analyzer import AcousticBrainzNG, FileJob, FileResult, TrackResult, get_instance
from .cache import album_key_entry
from .constants import FINGERPRINT_COMMENT, PLUGIN_NAME
from .loudness import AlbumJob


@dataclass
class WorkItem:
    # Track or Cluster containing the files, updated after the analysis
    parent: Any
    files: List[Any]
    # Album whose loudness is used, None for files outside of albums
    album: Optional[Album] = None
    # Whether to measure the album loudness or only reuse a cached one
    measure_album: bool = False


def _real_album(album: Any) -> Optional[Album]:
    # The non-album tracks "album" is a subclass of Album but must not be
    # treated as an album
    return album if type(album) is Album else None


def collect_work(objs) -> List[WorkItem]:
    items: Dict[int, WorkItem] = {}
    seen_files = set()

    def add(parent, files, album=None, measure_album=False):
        files = [file for file in files if id(file) not in seen_files]
        if not files:
            return
        seen_files.update(id(file) for file in files)
        item = items.setdefault(id(parent), WorkItem(parent, [], album, measure_album))
        item.files.extend(files)

    # Albums are collected first so that a track selected together with its
    # album gets the album loudness measured
    for obj in objs:
        if isinstance(obj, Album):
            album = _real_album(obj)
            for track in obj.tracks:
                add(track, track.files, album, album is not None)

    for obj in objs:
        if isinstance(obj, Track):
            add(obj, obj.files, _real_album(obj.album))
        elif isinstance(obj, Cluster):
            add(obj, obj.files)
        elif isinstance(obj, File):
            parent = obj.parent_item
            album = _real_album(parent.album) if isinstance(parent, Track) else None
            add(parent, [obj], album)

    return list(items.values())


def file_job(file) -> FileJob:
    return FileJob(
        file=file,
        filename=file.filename,
        metadata={
            name: file.metadata.get(name)
            for name in ('musicbrainz_albumartistid', 'musicbrainz_releasegroupid', 'musicbrainz_albumid', 'musicbrainz_recordingid')
        },
    )


def album_job(album: Album, measure: bool) -> AlbumJob:
    files = []
    key = []
    for track in album.tracks:
        for file in track.files:
            files.append(file.filename)
            key.append(album_key_entry(file.filename, {'musicbrainz_recordingid': file.metadata.get('musicbrainz_recordingid')}))
    return AlbumJob(files=files, key=sorted(key), track_count=len(album.tracks), measure=measure)


def _item_name(item: WorkItem) -> str:
    if isinstance(item.parent, Track):
        return item.parent.metadata.get('title', 'Unknown Track')
    if len(item.files) == 1:
        file = item.files[0]
        return file.metadata.get('title') or os.path.basename(file.filename)
    if item.parent is not None and item.parent.metadata.get('album'):
        return item.parent.metadata['album']
    return f"{len(item.files)} files"


def _set_statusbar_message(api: PluginApi, message: str, *args) -> None:
    api.tagger.window.set_statusbar_message(message, *args, translate=None)


def apply_file_result(result: FileResult) -> None:
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


def _update_containers(item: WorkItem, update_album: bool = True) -> None:
    if item.parent is not None:
        item.parent.update()
    if update_album and item.album is not None:
        item.album.update()


class AnalysisBatch:
    def __init__(self, num_items: int):
        self.num_items = num_items
        self.current = 0

    def format_progress(self) -> str:
        if self.num_items <= 1:
            return ""
        self.current += 1
        return f" ({self.current}/{self.num_items})"


class AcousticBrainzNGAction(BaseAction):
    TITLE = "Analyze"
    MENU = (PLUGIN_NAME,)

    def callback(self, objs):
        plugin = get_instance()
        if plugin is None:
            return

        items = []
        for item in collect_work(objs):
            item.files = [file for file in item.files if file.filename not in plugin.in_flight]
            if item.files:
                items.append(item)

        if not items:
            return

        settings = plugin.settings()
        plugin.pool.setMaxThreadCount(settings.max_concurrent_analyses)
        batch = AnalysisBatch(len(items))
        album_jobs: Dict[tuple, AlbumJob] = {}

        if batch.num_items == 1:
            _set_statusbar_message(self.api, 'Analyzing "%s" with %s...', _item_name(items[0]), PLUGIN_NAME)
        else:
            _set_statusbar_message(self.api, 'Analyzing %i tracks with %s...', batch.num_items, PLUGIN_NAME)

        self.api.logger.debug(f"Analyzing {sum(len(item.files) for item in items)} files from {batch.num_items} tracks with {PLUGIN_NAME}")

        for item in items:
            job_for_album = None
            if item.album is not None:
                album_key = (id(item.album), item.measure_album)
                if album_key not in album_jobs:
                    album_jobs[album_key] = album_job(item.album, item.measure_album)
                job_for_album = album_jobs[album_key]

            plugin.in_flight.update(file.filename for file in item.files)
            thread.run_task(
                partial(plugin.analyze_track, settings, [file_job(file) for file in item.files], job_for_album),
                partial(_analysis_callback, plugin, batch, item),
                thread_pool=plugin.pool,
            )


def _analysis_callback(plugin: AcousticBrainzNG, batch: AnalysisBatch, item: WorkItem, result: Optional[TrackResult] = None, error=None):
    for file in item.files:
        plugin.in_flight.discard(file.filename)

    if result is not None and result.cancelled:
        return

    api = plugin.api
    progress = batch.format_progress()
    name = _item_name(item)

    if error is None and result:
        for file_result in result.files:
            apply_file_result(file_result)
        _update_containers(item)

        processed = sum(1 for file_result in result.files if file_result.processed)
        has_errors = any(file_result.errors for file_result in result.files)

        if processed == len(result.files) and not has_errors:
            if item.album is not None:
                album_name = item.album.metadata.get('album', 'Unknown Album')
                _set_statusbar_message(api, 'Successfully analyzed "%s" from "%s"%s.', name, album_name, progress)
            else:
                _set_statusbar_message(api, 'Successfully analyzed "%s"%s.', name, progress)
        elif processed > 0:
            _set_statusbar_message(api, 'Partially analyzed "%s" with warnings%s.', name, progress)
        else:
            _set_statusbar_message(api, 'Failed to analyze "%s"%s.', name, progress)
    else:
        error_msg = str(error) if error else "Unknown error"
        api.logger.error(f"Analysis failed for {name}: {error_msg}")
        _set_statusbar_message(api, 'Failed to analyze "%s"%s.', name, progress)


class AcousticBrainzNGDeleteCacheAction(BaseAction):
    TITLE = "Delete cache"
    MENU = (PLUGIN_NAME,)

    def callback(self, objs):
        plugin = get_instance()
        if plugin is None:
            return

        items = collect_work(objs)
        if not items:
            return

        num_items = len(items)
        name = _item_name(items[0])

        if num_items == 1:
            _set_statusbar_message(self.api, 'Deleting %s cache for "%s"...', PLUGIN_NAME, name)
        else:
            _set_statusbar_message(self.api, 'Deleting %s cache for %i tracks...', PLUGIN_NAME, num_items)

        # The album loudness is only deleted when the whole album is selected
        jobs = [(file_job(file), item.measure_album) for item in items for file in item.files]

        self.api.logger.debug(f"Deleting {PLUGIN_NAME} cache for {len(jobs)} files from {num_items} tracks")

        def done(result=None, error=None):
            if num_items == 1:
                _set_statusbar_message(self.api, 'Deleted %s cache for "%s".', PLUGIN_NAME, name)
            else:
                _set_statusbar_message(self.api, 'Deleted %s cache for %i tracks.', PLUGIN_NAME, num_items)

        # Computing the cache folder of a file without a recording MBID reads
        # the whole file, keep that off the main thread
        thread.run_task(partial(plugin.delete_cache, plugin.settings(), jobs), done)


def apply_cached_results(api: PluginApi, track: Track, file: File) -> None:
    """Applies the results of a previous analysis when a file is matched to a
    track, since Picard replaces the file's metadata with the track's"""
    plugin = get_instance()
    if plugin is None or file.filename in plugin.in_flight:
        return

    settings = plugin.settings()
    # Files without a recording MBID would need their audio hashed to find
    # their cache, which is too expensive to do for every loaded file
    if not settings.auto_apply_cached or not file.metadata.get('musicbrainz_recordingid'):
        return

    album = _real_album(track.album)
    item = WorkItem(track, [file], album)
    thread.run_task(
        partial(plugin.load_cached, settings, file_job(file), album_job(album, False) if album else None),
        partial(_cached_results_callback, item),
    )


def _cached_results_callback(item: WorkItem, result: Optional[FileResult] = None, error=None):
    if result is None or get_instance() is None:
        return
    # The file may have been moved elsewhere in the meantime
    if result.file.parent_item is not item.parent:
        return
    apply_file_result(result)
    # This runs for every file of an album while it loads, updating the album
    # each time would be wasteful
    _update_containers(item, update_album=False)
