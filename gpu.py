# Only modules that Picard itself uses: its Windows and macOS builds only
# include those of the standard library, e.g. no wave, glob or ctypes.util
import json
import os
import platform
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .constants import DEFAULT_GPU_PREFERENCE, ENV, WEBGPU_LIBRARIES
from .settings import exe

VENDORS = {
    "10de": "NVIDIA",
    "1002": "AMD",
    "8086": "Intel",
    "106b": "Apple",
    "5143": "Qualcomm",
}

# Database of PCI device names, used for the GPU names on Linux
PCI_IDS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids")

# Where Linux distributions install the Vulkan loader
LIBRARY_DIRECTORIES = ("/usr/lib64", "/usr/lib", "/lib64", "/lib", "/usr/local/lib",
                       "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu")


@dataclass
class GpuInfo:
    # Names of the GPUs found
    names: List[str] = field(default_factory=list)
    # Why the GPU can't be used even though one was found, if it can't
    problem: Optional[str] = None

    @property
    def usable(self) -> bool:
        return bool(self.names) and self.problem is None


def _run(arguments: List[str]) -> str:
    try:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=ENV,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def pci_name(vendor_id: str, device_id: str) -> str:
    """Name of a PCI device, e.g. "NVIDIA GeForce RTX 4060 Laptop GPU", falls
    back to the vendor name"""
    vendor = VENDORS.get(vendor_id, f"GPU {vendor_id}")
    for path in PCI_IDS:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                in_vendor = False
                for line in f:
                    if line.startswith(vendor_id + "  "):
                        in_vendor = True
                    elif in_vendor and line.startswith("\t" + device_id + "  "):
                        name = line.strip().split("  ", 1)[1]
                        # e.g. "AD107M [GeForce RTX 4060 Max-Q / Mobile]"
                        if "[" in name and name.endswith("]"):
                            name = name[name.index("[") + 1:-1]
                        return f"{vendor} {name}"
                    elif in_vendor and not line.startswith(("\t", "#")):
                        break
        except OSError:
            continue
    return f"{vendor} GPU"


def _has_vulkan() -> bool:
    try:
        import ctypes.util
        return ctypes.util.find_library("vulkan") is not None
    except ImportError:
        return any(os.path.exists(os.path.join(directory, "libvulkan.so.1")) for directory in LIBRARY_DIRECTORIES)


def _linux_gpus() -> GpuInfo:
    info = GpuInfo()
    try:
        cards = os.listdir("/sys/class/drm")
    except OSError:
        cards = []
    # cardN-<connector> entries are the outputs of a card
    for card in sorted(card for card in cards if card.startswith("card") and card[4:].isdigit()):
        device = os.path.join("/sys/class/drm", card, "device")
        try:
            with open(os.path.join(device, "vendor")) as f:
                vendor_id = f.read().strip().lower().removeprefix("0x")
            with open(os.path.join(device, "device")) as f:
                device_id = f.read().strip().lower().removeprefix("0x")
        except OSError:
            continue
        if vendor_id in VENDORS:
            info.names.append(pci_name(vendor_id, device_id))
    if info.names and not _has_vulkan():
        info.problem = "Vulkan isn't installed (libvulkan.so.1, e.g. the vulkan-loader or libvulkan1 package)"
    return info


def _windows_gpus() -> GpuInfo:
    output = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"])
    names = [line.strip() for line in output.splitlines() if line.strip()]
    # Not real GPUs
    names = [name for name in names if not any(word in name for word in ("Basic Display", "Basic Render", "Remote", "Virtual", "Hyper-V"))]
    return GpuInfo(names)


def _macos_gpus() -> GpuInfo:
    if platform.machine() == "arm64":
        return GpuInfo(["Apple GPU"])
    try:
        displays = json.loads(_run(["system_profiler", "SPDisplaysDataType", "-json"]) or "{}")
    except ValueError:
        displays = {}
    names = [display.get("sppci_model", "GPU") for display in displays.get("SPDisplaysDataType", [])]
    return GpuInfo(names)


_detected: Optional[GpuInfo] = None


def detect_gpus() -> GpuInfo:
    """The GPUs the tools could use, found without the tools. Can take a second
    on Windows, call it outside of the main thread. The result is cached"""
    global _detected
    if _detected is None:
        if sys.platform == "darwin":
            _detected = _macos_gpus()
        elif sys.platform == "win32":
            _detected = _windows_gpus()
        else:
            _detected = _linux_gpus()
    return _detected


def needs_gpu_tools() -> bool:
    """Whether the GPU needs the "gpu" tools, on macOS all tools can use it"""
    return sys.platform != "darwin"


def tools_support_gpu(binaries_path: str) -> bool:
    """Whether the tools in binaries_path can run the models on the GPU"""
    if not needs_gpu_tools():
        return True
    return any(os.path.exists(os.path.join(binaries_path, library)) for library in WEBGPU_LIBRARIES)


@dataclass
class ToolsDevice:
    name: str
    discrete: bool


def tools_gpus(binaries_path: str) -> Optional[List[ToolsDevice]]:
    """The GPUs the tools in binaries_path can use, None when they can't tell
    (macOS, or tools without GPU support)"""
    if not needs_gpu_tools() or not tools_support_gpu(binaries_path):
        return None
    # The tools only know the PCI IDs, Windows has the names
    names = detect_gpus().names if sys.platform == "win32" else []
    devices = []
    for line in _run([os.path.join(binaries_path, exe("abng_musicnn")), "--list-devices"]).splitlines():
        try:
            device = json.loads(line)
        except ValueError:
            continue
        vendor_id, device_id = device.get("vendor_id", ""), device.get("device_id", "")
        if sys.platform == "win32":
            vendor = VENDORS.get(vendor_id, "GPU")
            name = next((name for name in names if vendor.lower() in name.lower()), f"{vendor} GPU")
        else:
            name = pci_name(vendor_id, device_id)
        devices.append(ToolsDevice(name, bool(device.get("discrete"))))
    return devices


# Whether abng_musicnn supports --gpu-preference (tools-4 and newer), by path
# and modification time of the binary
_gpu_preference_support: Dict[Tuple[str, float], bool] = {}


def tools_support_gpu_preference(binaries_path: str) -> bool:
    """Whether the tools in binaries_path can choose between several GPUs"""
    binary = os.path.join(binaries_path, exe("abng_musicnn"))
    try:
        key = (binary, os.path.getmtime(binary))
    except OSError:
        return False
    if key not in _gpu_preference_support:
        # Without arguments the tool prints its usage, which lists its options
        try:
            result = subprocess.run(
                [binary],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=ENV,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            _gpu_preference_support[key] = "--gpu-preference" in result.stdout + result.stderr
        except (OSError, subprocess.SubprocessError):
            return False
    return _gpu_preference_support[key]


def gpu_preference_arguments(binaries_path: str, preference: str) -> List[str]:
    """The abng_musicnn arguments for the GPU preference. Empty for the default,
    or when the tools are too old to know it"""
    if needs_gpu_tools() and preference != DEFAULT_GPU_PREFERENCE and tools_support_gpu_preference(binaries_path):
        return ["--gpu-preference", preference]
    return []


@dataclass
class ToolsGpu:
    # Whether a test run of the tools used the GPU
    used: bool
    # The GPUs the tools found, None when they can't tell (macOS)
    devices: Optional[List[str]] = None
    # Why the tools didn't use the GPU
    problem: Optional[str] = None


def _write_silence(path: str, seconds: int = 5, rate: int = 16000) -> None:
    """Writes a 16 bit mono WAV file of silence"""
    data = b"\x00\x00" * rate * seconds
    header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
              + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
              + b"data" + struct.pack("<I", len(data)))
    with open(path, "wb") as output:
        output.write(header + data)


def check_tools_gpu(binaries_path: str, model_path: str, preference: str = DEFAULT_GPU_PREFERENCE) -> ToolsGpu:
    """Runs a model with the tools in binaries_path on the GPU, to find out
    whether they can use it"""
    binary = os.path.join(binaries_path, exe("abng_musicnn"))
    gpus = tools_gpus(binaries_path)
    devices = None if gpus is None else [gpu.name + (" (discrete)" if gpu.discrete else "") for gpu in gpus]

    with tempfile.TemporaryDirectory(prefix="abng-gpu-check-") as directory:
        audio = os.path.join(directory, "silence.wav")
        _write_silence(audio)
        try:
            result = subprocess.run(
                [binary, "--device", "gpu", *gpu_preference_arguments(binaries_path, preference),
                 audio, model_path, os.path.join(directory, "output.json")],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=ENV,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
        except (OSError, subprocess.SubprocessError) as e:
            return ToolsGpu(False, devices, f"abng_musicnn doesn't run: {e}")

    if "device: gpu" in result.stdout:
        return ToolsGpu(True, devices)
    for line in result.stderr.splitlines():
        if "GPU unavailable (" in line:
            return ToolsGpu(False, devices, line.split("GPU unavailable (", 1)[1].rsplit("), using", 1)[0])
    return ToolsGpu(False, devices, (result.stderr or result.stdout).strip() or f"abng_musicnn failed with exit code {result.returncode}")
