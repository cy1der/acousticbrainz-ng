import ctypes.util
import glob
import json
import os
import platform
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from typing import List, Optional

from .constants import ENV, WEBGPU_LIBRARIES
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


def _linux_gpus() -> GpuInfo:
    info = GpuInfo()
    for device in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
        # cardN-<connector> entries are the outputs of a card
        if "-" in os.path.basename(os.path.dirname(device)):
            continue
        try:
            with open(os.path.join(device, "vendor")) as f:
                vendor_id = f.read().strip().lower().removeprefix("0x")
            with open(os.path.join(device, "device")) as f:
                device_id = f.read().strip().lower().removeprefix("0x")
        except OSError:
            continue
        if vendor_id in VENDORS:
            info.names.append(pci_name(vendor_id, device_id))
    if info.names and not ctypes.util.find_library("vulkan"):
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
class ToolsGpu:
    # Whether a test run of the tools used the GPU
    used: bool
    # The GPUs the tools found, None when they can't tell (macOS)
    devices: Optional[List[str]] = None
    # Why the tools didn't use the GPU
    problem: Optional[str] = None


def _write_silence(path: str, seconds: int = 5) -> None:
    with wave.open(path, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 16000 * seconds)


def check_tools_gpu(binaries_path: str, model_path: str) -> ToolsGpu:
    """Runs a model with the tools in binaries_path on the GPU, to find out
    whether they can use it"""
    binary = os.path.join(binaries_path, exe("abng_musicnn"))
    devices = None
    if needs_gpu_tools():
        devices = []
        for line in _run([binary, "--list-devices"]).splitlines():
            try:
                device = json.loads(line)
            except ValueError:
                continue
            vendor_id, device_id = device.get("vendor_id", ""), device.get("device_id", "")
            name = pci_name(vendor_id, device_id) if sys.platform.startswith("linux") else VENDORS.get(vendor_id, "GPU")
            devices.append(name + (" (discrete)" if device.get("discrete") else ""))

    with tempfile.TemporaryDirectory(prefix="abng-gpu-check-") as directory:
        audio = os.path.join(directory, "silence.wav")
        _write_silence(audio)
        try:
            result = subprocess.run(
                [binary, "--device", "gpu", audio, model_path, os.path.join(directory, "output.json")],
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
