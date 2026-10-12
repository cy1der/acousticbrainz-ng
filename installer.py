import hashlib
import os
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.parse
import urllib.request
import zipfile
from typing import Callable, Dict, Optional

from .constants import (
    ENV,
    REQUIRED_BINARIES,
    TOOLS_CHECKSUMS,
    TOOLS_PLATFORM_ALIASES,
    TOOLS_RELEASE,
    TOOLS_URL,
)
from .settings import exe

# Written next to the installed tools, so the plugin knows which release they are
RELEASE_FILE = "TOOLS_RELEASE"

CHUNK_SIZE = 1 << 20

WORK_PREFIX = ".abng-install-"


class InstallCancelled(Exception):
    pass


class InstallError(Exception):
    pass


def archive_platform(platform: str) -> Optional[str]:
    """The platform of the prebuilt tools that run on this platform, if any"""
    platform = TOOLS_PLATFORM_ALIASES.get(platform, platform)
    return platform if platform in TOOLS_CHECKSUMS else None


def archive_name(platform: str) -> str:
    extension = "zip" if platform.startswith("windows-") else "tar.xz"
    return f"abng-tools-{TOOLS_RELEASE}-{platform}.{extension}"


def installed_release(directory: str) -> Optional[int]:
    try:
        with open(os.path.join(directory, RELEASE_FILE), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def proxy_url(setting) -> Optional[str]:
    """The URL of the HTTP proxy configured in Picard, if any"""
    if not setting["use_proxy"]:
        return None
    if setting["proxy_type"] != "http":
        raise InstallError("Only HTTP proxies are supported, download the tools manually, see the README")

    credentials = ""
    if setting["proxy_username"]:
        credentials = urllib.parse.quote(setting["proxy_username"], safe="")
        if setting["proxy_password"]:
            credentials += ":" + urllib.parse.quote(setting["proxy_password"], safe="")
        credentials += "@"
    return f"http://{credentials}{setting['proxy_server_host']}:{setting['proxy_server_port']}"


class ToolsInstaller:
    """Downloads the prebuilt tools of this plugin's tools release, verifies
    and installs them. The tools already in the destination are only replaced
    once the new ones are known to work"""

    def __init__(self, platform: str, destination: str, proxy: Optional[str] = None,
                 progress: Optional[Callable[[str, int, int], None]] = None,
                 cancelled: Optional[threading.Event] = None, url: str = TOOLS_URL,
                 checksums: Dict[str, str] = TOOLS_CHECKSUMS):
        self.platform = platform
        self.destination = destination
        self.proxy = proxy
        self.progress = progress or (lambda stage, done, total: None)
        self.cancelled = cancelled or threading.Event()
        self.url = url
        self.checksums = checksums

    def _check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise InstallCancelled()

    def install(self) -> None:
        if self.platform not in self.checksums:
            raise InstallError(f"There are no prebuilt tools for {self.platform}")

        name = archive_name(self.platform)
        parent = os.path.dirname(self.destination)
        os.makedirs(parent, exist_ok=True)
        # Left behind when Picard quit during an installation
        for entry in os.listdir(parent):
            if entry.startswith(WORK_PREFIX):
                shutil.rmtree(os.path.join(parent, entry), ignore_errors=True)
        # Next to the destination, so the final move is a rename on the same file system
        work = tempfile.mkdtemp(prefix=WORK_PREFIX, dir=parent)
        try:
            archive = os.path.join(work, name)
            self._download(self.url.format(release=TOOLS_RELEASE, name=name), archive, self.checksums[self.platform])

            self.progress("extract", 0, 0)
            staging = os.path.join(work, "staging")
            if name.endswith(".zip"):
                self._extract_zip(archive, staging)
            else:
                self._extract_tar(archive, staging)
            tools = os.path.join(staging, self.platform)

            self.progress("check", 0, 0)
            self._check_tools(tools)
            with open(os.path.join(tools, RELEASE_FILE), "w", encoding="utf-8") as f:
                f.write(f"{TOOLS_RELEASE}\n")

            self._check_cancelled()
            if os.path.exists(self.destination):
                os.rename(self.destination, os.path.join(work, "previous"))
            os.rename(tools, self.destination)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _download(self, url: str, path: str, checksum: str) -> None:
        handlers = [urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})] if self.proxy else []
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(url, headers={"User-Agent": "AcousticBrainz-ng Picard plugin"})
        digest = hashlib.sha256()

        try:
            with opener.open(request, timeout=60) as response, open(path, "wb") as output:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                self.progress("download", done, total)
                while True:
                    self._check_cancelled()
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    self.progress("download", done, total)
        except OSError as e:
            raise InstallError(f"Downloading {url} failed: {e}") from e

        if digest.hexdigest() != checksum:
            raise InstallError(f"The download from {url} is corrupted or was tampered with (checksum mismatch)")

    def _member_path(self, name: str) -> str:
        # Only accept the files of the archive's platform directory
        path = posixpath.normpath(name.replace("\\", "/"))
        if path.startswith("/") or ":" in path or ".." in path.split("/") or not (path == self.platform or path.startswith(self.platform + "/")):
            raise InstallError(f"Unexpected file in the archive: {name}")
        return path

    def _extract_tar(self, archive: str, staging: str) -> None:
        try:
            with tarfile.open(archive, "r:xz") as tar:
                members = tar.getmembers()
                for member in members:
                    path = self._member_path(member.name)
                    if member.issym() or member.islnk():
                        # TensorFlow's versioned library names are symlinks to each other
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(path), member.linkname)) if member.issym() else posixpath.normpath(member.linkname)
                        self._member_path(target)
                    elif not (member.isfile() or member.isdir()):
                        raise InstallError(f"Unexpected file in the archive: {member.name}")
                self._check_cancelled()
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(staging, members, filter="data")
                else:
                    tar.extractall(staging, members)
        except (tarfile.TarError, OSError) as e:
            raise InstallError(f"Extracting {os.path.basename(archive)} failed: {e}") from e

    def _extract_zip(self, archive: str, staging: str) -> None:
        try:
            with zipfile.ZipFile(archive) as zip_file:
                for info in zip_file.infolist():
                    self._member_path(info.filename)
                self._check_cancelled()
                zip_file.extractall(staging)
        except (zipfile.BadZipFile, OSError) as e:
            raise InstallError(f"Extracting {os.path.basename(archive)} failed: {e}") from e

    def _check_tools(self, directory: str) -> None:
        # Without arguments the tools print their usage, anything else means
        # they can't be loaded, e.g. because of a missing library
        for binary in REQUIRED_BINARIES:
            self._check_cancelled()
            path = os.path.join(directory, exe(binary))
            try:
                result = subprocess.run(
                    [path],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=ENV,
                    timeout=120,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
            except (OSError, subprocess.SubprocessError) as e:
                raise InstallError(f"{binary} doesn't run: {e}") from e
            if "Usage:" not in result.stdout + result.stderr:
                raise InstallError(f"{binary} doesn't run: {(result.stdout + result.stderr).strip()}")
