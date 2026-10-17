#!/usr/bin/env python3
"""Checks that the repository is ready to be released as a plugin version.

Usage: check-release.py TAG

TAG is the version tag to release, e.g. v3.2.0. Checks that it matches the
version in MANIFEST.toml and the install instructions in the README, that the
checksums in constants.py match the SHA256SUMS of the tools release the
plugin needs, and that the plugin only imports standard library modules
included in Picard's Windows and macOS builds. Run by the release workflow,
run it before tagging too. Needs Python 3.11 or newer and network access.
"""

import ast
import importlib.util
import os
import re
import sys
import tomllib
import urllib.request

REPOSITORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Picard's Windows and macOS builds are frozen with PyInstaller, they only
# include the standard library modules Picard itself uses. These are the ones
# the plugin is known to work with there. Before adding one, check that it is
# in Picard's Windows build. Imports guarded by "except ImportError" are allowed
STDLIB_MODULES = {
    "concurrent.futures", "dataclasses", "functools", "hashlib", "json", "math", "os", "platform",
    "posixpath", "re", "shutil", "struct", "subprocess", "sys", "tarfile", "tempfile", "threading",
    "typing", "urllib.parse", "urllib.request", "zipfile", "zlib",
}


def _guarded(node: ast.AST, parents: dict) -> bool:
    """Whether an import is inside a try that catches ImportError"""
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.Try) and child in parent.body:
            for handler in parent.handlers:
                names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                if handler.type is None or any(isinstance(name, ast.Name) and name.id in ("ImportError", "ModuleNotFoundError", "Exception") for name in names):
                    return True
        child, parent = parent, parents.get(parent)
    return False


def check_imports() -> list:
    """Standard library imports of the plugin missing from STDLIB_MODULES"""
    errors = []
    for name in sorted(os.listdir(REPOSITORY)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(REPOSITORY, name)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module.split(".")[0] in sys.stdlib_module_names and module not in STDLIB_MODULES and not _guarded(node, parents):
                    errors.append(f"{name}:{node.lineno} imports {module}, which Picard's Windows build might not include, "
                                  "see STDLIB_MODULES in scripts/check-release.py")
    return errors


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
    errors = check_imports()

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
        f"abng-tools-{release}-{platform}{'' if variant == 'cpu' else '-' + variant}.{'zip' if platform.startswith('windows-') else 'tar.xz'}": checksum
        for variant, checksums in constants.TOOLS_CHECKSUMS.items()
        for platform, checksum in checksums.items()
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
