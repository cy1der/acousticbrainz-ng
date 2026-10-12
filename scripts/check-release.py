#!/usr/bin/env python3
"""Checks that the repository is ready to be released as a plugin version.

Usage: check-release.py TAG

TAG is the version tag to release, e.g. v3.2.0. Checks that it matches the
version in MANIFEST.toml and the install instructions in the README, and that
the checksums in constants.py match the SHA256SUMS of the tools release the
plugin needs. Run by the release workflow,
run it before tagging too. Needs Python 3.11 or newer and network access.
"""

import importlib.util
import os
import re
import sys
import tomllib
import urllib.request

REPOSITORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_constants():
    # Only needs the standard library, unlike the rest of the plugin
    spec = importlib.util.spec_from_file_location("constants", os.path.join(REPOSITORY, "constants.py"))
    constants = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(constants)
    return constants


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    tag = sys.argv[1]
    errors = []

    with open(os.path.join(REPOSITORY, "MANIFEST.toml"), "rb") as f:
        version = tomllib.load(f)["version"]
    # Picard only updates plugins installed from a tag to tags that look like
    # version numbers
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        errors.append(f"The tag must look like v1.2.3, got {tag}")
    elif tag != f"v{version}":
        errors.append(f"The tag {tag} doesn't match the version {version} in MANIFEST.toml")

    with open(os.path.join(REPOSITORY, "README.md"), encoding="utf-8") as f:
        getting_started = re.search(r"^## Getting started$(.*?)^## ", f.read(), re.M | re.S)
    tags = set(re.findall(r"\bv\d+\.\d+\.\d+\b", getting_started.group(1) if getting_started else ""))
    if tags != {tag}:
        errors.append(f"Getting started in the README should only install {tag}, it mentions {', '.join(sorted(tags)) or 'none'}")

    constants = load_constants()
    release = constants.TOOLS_RELEASE
    url = constants.TOOLS_URL.format(release=release, name="SHA256SUMS")
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            published = {}
            for line in response.read().decode().splitlines():
                checksum, name = line.split()
                published[name] = checksum
    except OSError as e:
        raise SystemExit(f"Can't download {url}: {e}")

    # Named like installer.archive_name()
    pinned = {
        f"abng-tools-{release}-{platform}.{'zip' if platform.startswith('windows-') else 'tar.xz'}": checksum
        for platform, checksum in constants.TOOLS_CHECKSUMS.items()
    }
    for name in sorted(pinned.keys() | published.keys()):
        if name not in published:
            errors.append(f"{name} isn't in the tools-{release} release")
        elif name not in pinned:
            errors.append(f"{name} from the tools-{release} release has no checksum in constants.py")
        elif published[name] != pinned[name]:
            errors.append(f"The checksum of {name} in constants.py doesn't match the tools-{release} release")

    if errors:
        raise SystemExit("\n".join(errors))
    print(f"{tag} is ready, it uses tools-{release}")


if __name__ == "__main__":
    main()
