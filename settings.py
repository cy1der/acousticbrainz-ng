import os
import platform
import shutil
import sys
from dataclasses import dataclass
from typing import Optional

from picard.plugin3.api import PluginApi
from PyQt6 import QtCore

try:
    from picard.const.appdirs import cache_folder as _picard_cache_folder
except ImportError:
    def _picard_cache_folder() -> str:
        return QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.CacheLocation)


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
    auto_apply_cached: bool


def exe(name: str) -> str:
    return name + (".exe" if os.name == 'nt' else "")


def platform_name() -> str:
    """Name of the directory the build scripts install the binaries for this
    platform to, e.g. linux-x86_64"""
    if sys.platform == "darwin":
        system = "macos"
    elif sys.platform == "win32":
        system = "windows"
    else:
        system = "linux"

    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        machine = "x86_64"
    elif machine in ("arm64", "aarch64"):
        machine = "arm64"

    return f"{system}-{machine}"


def default_path(api: PluginApi, name: str) -> str:
    if name == "binaries_path":
        return str(api.plugin_dir / "bin" / platform_name())
    if name == "models_path":
        return str(api.plugin_dir / "models")
    if name == "cache_path":
        return os.path.join(_picard_cache_folder(), "acousticbrainz-ng")
    if name == "ffmpeg_path":
        return shutil.which("ffmpeg") or ""
    raise KeyError(name)


def resolve_path(api: PluginApi, name: str, value: Optional[str] = None) -> str:
    if value is None:
        value = api.plugin_config[name]
    return value or default_path(api, name)


def read_settings(api: PluginApi) -> Settings:
    cfg = api.plugin_config
    return Settings(
        binaries_path=resolve_path(api, "binaries_path"),
        ffmpeg_path=resolve_path(api, "ffmpeg_path"),
        models_path=resolve_path(api, "models_path"),
        cache_path=resolve_path(api, "cache_path"),
        max_musicnn_workers=cfg["max_musicnn_workers"],
        max_concurrent_analyses=cfg["max_concurrent_analyses"],
        replaygain_reference_loudness=cfg["replaygain_reference_loudness"],
        analyze_optional=cfg["analyze_optional"],
        save_raw=cfg["save_raw"],
        calculate_replaygain=cfg["calculate_replaygain"],
        save_fingerprint=cfg["analyze_optional"] and cfg["save_fingerprint"],
        auto_apply_cached=cfg["auto_apply_cached"],
    )
