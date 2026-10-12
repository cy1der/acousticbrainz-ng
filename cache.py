import hashlib
import json
import os
from typing import Dict, Optional

from .audio import AudioTools
from .settings import Settings


def cache_folder(settings: Settings, metadata: Dict[str, str], file_path: str, audio: AudioTools, create: bool = True) -> str:
    cache_base = settings.cache_path
    if not cache_base:
        raise ValueError("Cache path not configured")

    release_artist_mbid = metadata.get('musicbrainz_albumartistid') or 'NO_MBID'
    release_group_mbid = metadata.get('musicbrainz_releasegroupid') or 'NO_MBID'
    release_mbid = metadata.get('musicbrainz_albumid') or 'NO_MBID'
    recording_mbid = metadata.get('musicbrainz_recordingid')

    if not recording_mbid:
        recording_mbid = audio.get_audio_hash(settings, file_path)

    folder = os.path.join(
        str(cache_base),
        str(release_artist_mbid) if len(str(release_artist_mbid).encode('utf-8')) <= 255 else hashlib.sha256(str(release_artist_mbid).encode('utf-8')).hexdigest(),
        str(release_group_mbid),
        str(release_mbid),
        str(recording_mbid)
    )

    if create:
        os.makedirs(folder, exist_ok=True)

    return folder


def album_key_entry(filename: str, metadata: Dict[str, str]) -> str:
    """Identifies a file within an album's cached loudness"""
    return metadata.get('musicbrainz_recordingid') or filename


def read_json(path: str) -> Optional[Dict]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: str, data: Dict) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
