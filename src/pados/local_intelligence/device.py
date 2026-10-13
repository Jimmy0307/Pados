# SPDX-License-Identifier: AGPL-3.0-only
"""Device Profiler: a measured, provenance-carrying profile of the host PADOS runs on (description only).

Host facts come from OS APIs (``platform``, ``psutil``, ``/proc`` and ``/sys`` on Linux, the registry on Windows)
and, for NVIDIA GPUs, from ``nvidia-smi`` run with constant argv lists (no shell). Every tool output is
validated field by field; anything that does not validate is dropped and the probe is marked
``INVALID_OUTPUT`` instead of being trusted.

GPU discovery has four distinguishable outcomes (``GpuStatus``) so a missing GPU never breaks PADOS and is never
confused with a failed measurement. ``to_hardware_profile`` projects the measurement into the existing
``pados.resources.hardware.HardwareProfile`` consumed by ``StaticResourceInventory``; nothing is registered,
authorized or selected here.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import platform
import re
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import psutil  # type: ignore[import-untyped]  # declared runtime dependency; no stubs are pinned
from pydantic import BaseModel, ConfigDict, Field

from pados.execution.context import canonical_hash
from pados.local_intelligence.probes import Basis, ProbeOutcome, ProbeRecord, ProbeSession, display_text
from pados.local_intelligence.runtimes import RuntimeDescriptor
from pados.resources.hardware import (
    RUNTIME_ORDER,
    AcceleratorRuntime,
    Architecture,
    GpuInfo,
    GpuVendor,
    HardwareProfile,
    NodeRole,
    OperatingSystem,
    parse_architecture,
)

DEVICE_PROFILE_SCHEMA = "pados.local-intelligence.device-profile.v1"
PROFILER_VERSION = "1.0.0"
NVIDIA_QUERY_FIELDS = ("index", "name", "memory.total", "memory.used", "memory.free", "driver_version",
                       "pci.bus_id", "compute_cap", "utilization.gpu")
NVIDIA_BASIC_FIELDS = NVIDIA_QUERY_FIELDS[:7]
_WINDOWS_DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
_PCI_VENDORS = {"10de": GpuVendor.NVIDIA, "1002": GpuVendor.AMD, "1022": GpuVendor.AMD, "8086": GpuVendor.INTEL,
                "106b": GpuVendor.APPLE}
# Emulated display adapters of hypervisors and the OS fallback driver: never compute GPUs.
_VIRTUAL_DISPLAY_VENDORS = {"15ad": "VMware", "80ee": "VirtualBox", "1234": "QEMU", "1af4": "virtio", "1b36": "QEMU",
                            "1414": "Microsoft", "1013": "Cirrus (emulated)"}
_VIRTUAL_MARKERS = ("vmware", "virtualbox", "qemu", "kvm", "xen", "parallels", "virtual machine", "bochs", "hyper-v")
_VERSION = re.compile(r"^[0-9]{1,4}(\.[0-9]{1,5}){1,3}$")
_BUS_ID = re.compile(r"^[0-9A-Fa-f]{4,8}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7]$")
_COMPUTE_CAP = re.compile(r"^[0-9]{1,2}\.[0-9]$")
_PCI_ID = re.compile(r"^0x([0-9a-f]{4})$")
_VEN_IN_PNP = re.compile(r"VEN_([0-9A-F]{4})", re.IGNORECASE)  # INF ids may be lower case ("pci\\ven_10de&...")


class GpuStatus(StrEnum):
    GPU_PRESENT = "GPU_PRESENT"  # a GPU was enumerated by its vendor runtime with validated memory facts
    GPU_ABSENT = "GPU_ABSENT"  # platform enumeration succeeded and found no compute GPU
    GPU_UNAVAILABLE = "GPU_UNAVAILABLE"  # a GPU is there, but no usable runtime/driver facts could be obtained
    GPU_DISCOVERY_FAILED = "GPU_DISCOVERY_FAILED"  # the probes failed; nothing can be said either way


class FormFactor(StrEnum):
    VIRTUAL_MACHINE = "VIRTUAL_MACHINE"
    LAPTOP = "LAPTOP"  # a battery is reported
    NO_BATTERY_REPORTED = "NO_BATTERY_REPORTED"  # desktop/server, or a host that does not report batteries


class ComputeClass(StrEnum):
    DISCRETE_GPU = "DISCRETE_GPU"
    NO_USABLE_GPU = "NO_USABLE_GPU"
    GPU_UNVERIFIED = "GPU_UNVERIFIED"


class NetworkMode(StrEnum):
    """Addressing of the host's outbound route (no packet is sent to find it; not an internet reachability test)."""

    NO_ROUTE = "NO_ROUTE"
    LOOPBACK_ONLY = "LOOPBACK_ONLY"
    PRIVATE_NETWORK = "PRIVATE_NETWORK"
    PUBLIC_ADDRESS = "PUBLIC_ADDRESS"
    UNKNOWN = "UNKNOWN"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DisplayAdapter(_Model):
    """An adapter as the platform enumerates it (PCI class 0x03 on Linux, the display device class on Windows)."""

    vendor: GpuVendor | None
    vendor_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{4}$")
    name: str | None = Field(default=None, max_length=120)
    driver_version: str | None = Field(default=None, max_length=40)
    virtual: bool = False


class GpuDevice(_Model):
    index: int = Field(ge=0, le=64)
    vendor: GpuVendor
    name: str = Field(min_length=1, max_length=120)
    vram_total_mb: int = Field(ge=1, le=1_048_576)
    vram_used_mb: int | None = Field(default=None, ge=0, le=1_048_576)
    vram_free_mb: int | None = Field(default=None, ge=0, le=1_048_576)
    driver_version: str | None = None
    compute_capability: str | None = None
    pci_bus_id: str | None = None
    utilization_gpu_pct: int | None = Field(default=None, ge=0, le=100)
    source: str = "nvidia-smi"


class NvidiaFacts(_Model):
    smi_available: bool
    driver_version: str | None = None  # "DRIVER version", or "KMD version" where newer drivers deprecate it
    cuda_driver_version: str | None = None  # CUDA version of the driver stack ("CUDA Version" / "CUDA UMD Version")
    nvml_version: str | None = None
    version_labels: tuple[str, ...] = ()  # which nvidia-smi labels the versions were read from (provenance)


class CudaToolkit(_Model):
    nvcc_found: bool
    version: str | None = None


class GpuDiscovery(_Model):
    status: GpuStatus
    code: str | None = Field(default=None, max_length=120)
    devices: tuple[GpuDevice, ...] = ()
    adapters: tuple[DisplayAdapter, ...] = ()
    nvidia: NvidiaFacts = NvidiaFacts(smi_available=False)
    cuda_toolkit: CudaToolkit = CudaToolkit(nvcc_found=False)

    @property
    def cuda_available(self) -> bool:
        """CUDA-capable driver stack observed: an NVIDIA GPU enumerated and the driver reports a CUDA version."""
        return (self.status is GpuStatus.GPU_PRESENT and self.nvidia.cuda_driver_version is not None
                and any(device.vendor is GpuVendor.NVIDIA for device in self.devices))


class CpuFacts(_Model):
    model: str | None = Field(default=None, max_length=120)
    physical_cores: int | None = Field(default=None, ge=1, le=4096)
    logical_cores: int | None = Field(default=None, ge=1, le=8192)


class MemoryFacts(_Model):
    total_mb: int = Field(ge=0)
    available_mb: int = Field(ge=0)


class DiskFacts(_Model):
    path_label: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,39}$")  # a label, never the path (paths may name users)
    total_mb: int = Field(ge=0)
    free_mb: int = Field(ge=0)


class MachineFacts(_Model):
    hostname_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # stable reference without recording the name
    machine_ref: str | None = Field(default=None, pattern=r"^mach-[0-9a-f]{20}$")
    form_factor: FormFactor
    virtualization: str | None = Field(default=None, max_length=60)


class PythonFacts(_Model):
    version: str = Field(max_length=40)
    implementation: str = Field(max_length=40)
    bits: int


class DeviceProfile(_Model):
    schema_version: str = DEVICE_PROFILE_SCHEMA
    profiler_version: str = PROFILER_VERSION
    measured_at: datetime
    basis: Basis = Basis.PADOS_MEASURED
    os: OperatingSystem | None
    os_release: str | None = Field(default=None, max_length=80)
    os_version: str | None = Field(default=None, max_length=120)
    os_name: str | None = Field(default=None, max_length=120)
    architecture: Architecture | None
    machine: MachineFacts
    cpu: CpuFacts
    memory: MemoryFacts | None
    disk: DiskFacts | None
    gpu: GpuDiscovery
    python: PythonFacts
    network_mode: NetworkMode
    runtimes: tuple[RuntimeDescriptor, ...] = ()
    probes: tuple[ProbeRecord, ...] = ()

    @property
    def compute_class(self) -> ComputeClass:
        if self.gpu.status is GpuStatus.GPU_PRESENT:
            return ComputeClass.DISCRETE_GPU
        if self.gpu.status is GpuStatus.GPU_DISCOVERY_FAILED:
            return ComputeClass.GPU_UNVERIFIED
        return ComputeClass.NO_USABLE_GPU

    @property
    def machine_class(self) -> str:
        return f"{self.machine.form_factor.value}/{self.compute_class.value}"

    @property
    def profile_digest(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    def evidence(self) -> dict[str, Any]:
        """The JSON evidence document (profile plus derived classification and digest)."""
        return {**self.model_dump(mode="json"), "derived": {
            "machine_class": self.machine_class, "compute_class": self.compute_class.value,
            "cuda_available": self.gpu.cuda_available, "profile_digest": self.profile_digest}}


class HostFacts(Protocol):
    """OS access used by the profiler (a test seam; the default reads the real host)."""

    system: str

    def release(self) -> str: ...
    def version(self) -> str: ...
    def machine(self) -> str: ...
    def hostname(self) -> str: ...
    def read_text(self, path: str) -> str | None: ...
    def list_dir(self, path: str) -> list[str] | None: ...
    def cpu_counts(self) -> tuple[int | None, int | None]: ...
    def memory(self) -> tuple[int, int] | None: ...
    def disk(self, path: str) -> tuple[int, int] | None: ...
    def battery_present(self) -> bool | None: ...
    def registry_value(self, key: str, name: str) -> object: ...
    def registry_subkeys(self, key: str) -> list[str] | None: ...
    def outbound_address(self) -> str | None: ...


class LocalHost:
    """The real host. Windows registry reads are HKEY_LOCAL_MACHINE, read-only."""

    def __init__(self) -> None:
        self.system = platform.system()

    def release(self) -> str:
        return platform.release()

    def version(self) -> str:
        return platform.version()

    def machine(self) -> str:
        return platform.machine()

    def hostname(self) -> str:
        return socket.gethostname()

    def read_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")[:262_144]
        except OSError:
            return None

    def list_dir(self, path: str) -> list[str] | None:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return None

    def cpu_counts(self) -> tuple[int | None, int | None]:
        return psutil.cpu_count(logical=False), psutil.cpu_count(logical=True)

    def memory(self) -> tuple[int, int] | None:
        try:
            memory = psutil.virtual_memory()
        except OSError:
            return None
        return int(memory.total), int(memory.available)

    def disk(self, path: str) -> tuple[int, int] | None:
        import shutil

        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        return int(usage.total), int(usage.free)

    def battery_present(self) -> bool | None:
        try:
            return psutil.sensors_battery() is not None
        except (AttributeError, OSError, NotImplementedError):
            return None

    def registry_value(self, key: str, name: str) -> object:
        if sys.platform != "win32":
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                return winreg.QueryValueEx(handle, name)[0]
        except OSError:
            return None

    def registry_subkeys(self, key: str) -> list[str] | None:
        if sys.platform != "win32":
            return None
        import winreg

        names: list[str] = []
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                for index in range(256):
                    try:
                        names.append(winreg.EnumKey(handle, index))
                    except OSError:
                        break
        except OSError:
            return None
        return names

    def outbound_address(self) -> str | None:
        # A UDP "connect" only selects the route; no datagram is sent. 192.0.2.1 is TEST-NET-1 (RFC 5737).
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))
                return str(probe.getsockname()[0])
        except OSError:
            return None


_OS = {"Linux": OperatingSystem.LINUX, "Windows": OperatingSystem.WINDOWS, "Darwin": OperatingSystem.MACOS}


@dataclass
class DeviceProfiler:
    host: HostFacts
    session: ProbeSession
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    disk_path: str = "."
    disk_label: str = "PADOS_WORKSPACE"
    machine_ref: str | None = None

    @classmethod
    def local(cls, **kwargs: Any) -> DeviceProfiler:
        return cls(host=LocalHost(), session=ProbeSession(), **kwargs)

    def profile(self, runtimes: tuple[RuntimeDescriptor, ...] = ()) -> DeviceProfile:
        measured_at = self.clock()
        os_kind = _OS.get(self.host.system)
        try:
            architecture: Architecture | None = parse_architecture(self.host.machine())
        except ValueError:
            architecture = None
        gpu = self._gpu(os_kind)
        return DeviceProfile(
            measured_at=measured_at, os=os_kind, os_release=display_text(self.host.release(), max_length=80),
            os_version=display_text(self.host.version()), os_name=self._os_name(os_kind),
            architecture=architecture, machine=self._machine(os_kind), cpu=self._cpu(os_kind),
            memory=self._memory(), disk=self._disk(), gpu=gpu, python=PythonFacts(
                version=platform.python_version(), implementation=platform.python_implementation(),
                bits=64 if sys.maxsize > 2**32 else 32),
            network_mode=self._network(), runtimes=runtimes, probes=tuple(self.session.records))

    # -- host --------------------------------------------------------------------------------------------
    def _os_name(self, os_kind: OperatingSystem | None) -> str | None:
        if os_kind is OperatingSystem.LINUX:
            text = self.host.read_text("/etc/os-release") or ""
            match = re.search(r'^PRETTY_NAME="?([^"\n]+)"?$', text, re.MULTILINE)
            return display_text(match.group(1)) if match else None
        if os_kind is OperatingSystem.WINDOWS:
            key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
            build = self.host.registry_value(key, "CurrentBuildNumber")
            display = self.host.registry_value(key, "DisplayVersion")
            edition = self.host.registry_value(key, "EditionID")
            if isinstance(build, str) and build.isdigit():
                # The registry ProductName still says "Windows 10" on Windows 11; the build number decides.
                major = "11" if int(build) >= 22000 else "10"
                parts = [f"Windows {major}", display_text(edition, max_length=40), display_text(display, max_length=20),
                         f"build {build}"]
                return " ".join(part for part in parts if part)
        return None

    def _machine(self, os_kind: OperatingSystem | None) -> MachineFacts:
        started = self.session.clock()
        vendor = product = None
        if os_kind is OperatingSystem.LINUX:
            vendor = self.host.read_text("/sys/class/dmi/id/sys_vendor")
            product = self.host.read_text("/sys/class/dmi/id/product_name")
        elif os_kind is OperatingSystem.WINDOWS:
            bios = r"HARDWARE\DESCRIPTION\System\BIOS"
            vendor = _str(self.host.registry_value(bios, "SystemManufacturer"))
            product = _str(self.host.registry_value(bios, "SystemProductName"))
        identity = " ".join(item.strip() for item in (vendor, product) if item).casefold()
        virtual = next((marker for marker in _VIRTUAL_MARKERS if marker in identity), None)
        if "microsoft corporation" in identity and "virtual" not in identity:
            virtual = None  # Surface devices: Microsoft hardware, not a Hyper-V guest
        self.session.record("host.virtualization", ProbeOutcome.OK if identity else ProbeOutcome.NOT_FOUND, started,
                            code=None if identity else "DMI_IDENTITY_UNAVAILABLE")
        battery = self.host.battery_present()
        form = FormFactor.VIRTUAL_MACHINE if virtual else (
            FormFactor.LAPTOP if battery else FormFactor.NO_BATTERY_REPORTED)
        return MachineFacts(hostname_sha256=hashlib.sha256(self.host.hostname().casefold().encode()).hexdigest(),
                            machine_ref=self.machine_ref, form_factor=form,
                            virtualization=virtual.upper().replace(" ", "_") if virtual else None)

    def _cpu(self, os_kind: OperatingSystem | None) -> CpuFacts:
        physical, logical = self.host.cpu_counts()
        name = None
        if os_kind is OperatingSystem.LINUX:
            match = re.search(r"^model name\s*:\s*(.+)$", self.host.read_text("/proc/cpuinfo") or "", re.MULTILINE)
            name = match.group(1) if match else None
        elif os_kind is OperatingSystem.WINDOWS:
            name = _str(self.host.registry_value(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                                                 "ProcessorNameString"))
        return CpuFacts(model=display_text(name), physical_cores=physical if physical and physical > 0 else None,
                        logical_cores=logical if logical and logical > 0 else None)

    def _memory(self) -> MemoryFacts | None:
        figures = self.host.memory()
        if figures is None:
            return None
        return MemoryFacts(total_mb=figures[0] // 2**20, available_mb=figures[1] // 2**20)

    def _disk(self) -> DiskFacts | None:
        figures = self.host.disk(self.disk_path)
        if figures is None:
            return None
        return DiskFacts(path_label=self.disk_label, total_mb=figures[0] // 2**20, free_mb=figures[1] // 2**20)

    def _network(self) -> NetworkMode:
        address = self.host.outbound_address()
        if address is None:
            return NetworkMode.NO_ROUTE
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return NetworkMode.UNKNOWN
        if parsed.is_loopback:
            return NetworkMode.LOOPBACK_ONLY
        if parsed.is_private or parsed.is_link_local:
            return NetworkMode.PRIVATE_NETWORK
        return NetworkMode.PUBLIC_ADDRESS if parsed.is_global else NetworkMode.UNKNOWN

    # -- GPU ---------------------------------------------------------------------------------------------
    def _adapters(self, os_kind: OperatingSystem | None) -> tuple[DisplayAdapter, ...] | None:
        started = self.session.clock()
        adapters: list[DisplayAdapter] = []
        if os_kind is OperatingSystem.LINUX:
            root = "/sys/bus/pci/devices"
            entries = self.host.list_dir(root)
            if entries is None:
                self.session.record("platform.display-adapters", ProbeOutcome.NOT_FOUND, started, code="PCI_SYSFS_UNAVAILABLE")
                return None
            for entry in entries:
                device_class = (self.host.read_text(f"{root}/{entry}/class") or "").strip()
                if not device_class.startswith("0x03"):
                    continue
                vendor_match = _PCI_ID.fullmatch((self.host.read_text(f"{root}/{entry}/vendor") or "").strip())
                device_match = _PCI_ID.fullmatch((self.host.read_text(f"{root}/{entry}/device") or "").strip())
                vendor_id = vendor_match.group(1) if vendor_match else None
                adapters.append(_adapter(vendor_id, f"PCI {vendor_id or '????'}:{device_match.group(1) if device_match else '????'}", None))
        elif os_kind is OperatingSystem.WINDOWS:
            subkeys = self.host.registry_subkeys(_WINDOWS_DISPLAY_CLASS)
            if subkeys is None:
                self.session.record("platform.display-adapters", ProbeOutcome.NOT_FOUND, started, code="DISPLAY_CLASS_UNAVAILABLE")
                return None
            for subkey in subkeys:
                if not re.fullmatch(r"[0-9]{4}", subkey):
                    continue
                key = f"{_WINDOWS_DISPLAY_CLASS}\\{subkey}"
                description = _str(self.host.registry_value(key, "DriverDesc"))
                if description is None:
                    continue
                pnp = _str(self.host.registry_value(key, "MatchingDeviceId")) or ""
                match = _VEN_IN_PNP.search(pnp)
                driver = _str(self.host.registry_value(key, "DriverVersion"))
                adapters.append(_adapter(match.group(1).lower() if match else None, description,
                                         driver if driver and _VERSION.fullmatch(driver) else None))
        else:
            self.session.record("platform.display-adapters", ProbeOutcome.UNSUPPORTED_PLATFORM, started)
            return None
        self.session.record("platform.display-adapters", ProbeOutcome.OK, started)
        return tuple(adapters)

    def _gpu(self, os_kind: OperatingSystem | None) -> GpuDiscovery:
        adapters = self._adapters(os_kind)
        devices, smi_code = self._nvidia_devices()
        nvidia = self._nvidia_facts(smi_available=smi_code != "NVIDIA_SMI_NOT_FOUND")
        toolkit = self._cuda_toolkit() if devices else CudaToolkit(nvcc_found=False)
        listed = adapters or ()
        kwargs: dict[str, Any] = {"adapters": listed, "nvidia": nvidia, "cuda_toolkit": toolkit}
        if devices:
            return GpuDiscovery(status=GpuStatus.GPU_PRESENT, devices=devices, **kwargs)
        compute_vendors = sorted({a.vendor.value for a in listed if a.vendor is not None and not a.virtual})
        if adapters is None:
            # No platform enumeration; only a definitive NVIDIA answer could have settled it.
            return GpuDiscovery(status=GpuStatus.GPU_DISCOVERY_FAILED, code=f"PLATFORM_ENUMERATION_FAILED;{smi_code}",
                                **kwargs)
        if GpuVendor.NVIDIA.value in compute_vendors:
            failed = smi_code in {"NVIDIA_SMI_OUTPUT_INVALID", "NVIDIA_SMI_TIMED_OUT"}
            return GpuDiscovery(status=GpuStatus.GPU_DISCOVERY_FAILED if failed else GpuStatus.GPU_UNAVAILABLE,
                                code=smi_code, **kwargs)
        if compute_vendors:
            return GpuDiscovery(status=GpuStatus.GPU_UNAVAILABLE,
                                code="GPU_RUNTIME_PROBE_NOT_IMPLEMENTED:" + "|".join(compute_vendors), **kwargs)
        if any(adapter.vendor_id is None and not adapter.virtual for adapter in listed):
            # An adapter we could not identify is never evidence of absence.
            return GpuDiscovery(status=GpuStatus.GPU_UNAVAILABLE, code=f"GPU_VENDOR_UNIDENTIFIED;{smi_code}", **kwargs)
        return GpuDiscovery(status=GpuStatus.GPU_ABSENT, code="NO_COMPUTE_GPU_ENUMERATED", **kwargs)

    def _nvidia_devices(self) -> tuple[tuple[GpuDevice, ...], str]:
        for probe_id, fields in (("nvidia-smi.query-gpu", NVIDIA_QUERY_FIELDS),
                                 ("nvidia-smi.query-gpu-basic", NVIDIA_BASIC_FIELDS)):
            output = self.session.command(probe_id, ("nvidia-smi", f"--query-gpu={','.join(fields)}",
                                                     "--format=csv,noheader,nounits"))
            record = self.session.records[-1]
            if record.outcome is ProbeOutcome.NOT_FOUND:
                return (), "NVIDIA_SMI_NOT_FOUND"
            if record.outcome is ProbeOutcome.TIMED_OUT:
                return (), "NVIDIA_SMI_TIMED_OUT"
            if output is None:
                continue  # e.g. an older driver rejecting compute_cap: retry with the basic field set
            devices = _parse_nvidia(output, fields)
            if devices is None:
                self.session.invalid(probe_id, "NVIDIA_SMI_OUTPUT_INVALID")
                return (), "NVIDIA_SMI_OUTPUT_INVALID"
            return devices, "NVIDIA_SMI_OK" if devices else "NVIDIA_SMI_NO_DEVICES"
        return (), "NVIDIA_DRIVER_UNAVAILABLE"

    def _nvidia_facts(self, *, smi_available: bool) -> NvidiaFacts:
        if not smi_available:
            return NvidiaFacts(smi_available=False)
        output = self.session.command("nvidia-smi.version", ("nvidia-smi", "--version"))
        text = output.decode("utf-8", errors="replace") if output else ""
        labels: list[str] = []
        facts: dict[str, str | None] = {}
        for key, candidates in _NVIDIA_VERSION_LABELS:
            facts[key] = _first_version(text, candidates, labels)
        if facts["cuda_driver_version"] is None or facts["driver_version"] is None:
            header = self.session.command("nvidia-smi.summary", ("nvidia-smi",))
            summary = header.decode("utf-8", errors="replace") if header else ""
            for key, candidates in _NVIDIA_VERSION_LABELS[:2]:
                if facts[key] is None:
                    facts[key] = _first_version(summary, candidates, labels, source="summary")
        return NvidiaFacts(smi_available=True, version_labels=tuple(labels), **facts)

    def _cuda_toolkit(self) -> CudaToolkit:
        output = self.session.command("nvcc.version", ("nvcc", "--version"))
        if output is None:
            return CudaToolkit(nvcc_found=self.session.records[-1].outcome is not ProbeOutcome.NOT_FOUND)
        match = re.search(r"release ([0-9]{1,3}\.[0-9]{1,3})", output.decode("utf-8", errors="replace"))
        return CudaToolkit(nvcc_found=True, version=match.group(1) if match else None)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _adapter(vendor_id: str | None, name: str | None, driver: str | None) -> DisplayAdapter:
    virtual = vendor_id in _VIRTUAL_DISPLAY_VENDORS
    vendor = None if virtual or vendor_id is None else _PCI_VENDORS.get(vendor_id, GpuVendor.OTHER)
    return DisplayAdapter(vendor=vendor, vendor_id=vendor_id, name=display_text(name), driver_version=driver,
                          virtual=virtual)


# nvidia-smi label variants, in preference order. Drivers from the 61x series print "DRIVER version" and
# "CUDA version" as "Deprecated, see ..." and report "KMD version" / "CUDA UMD version" instead.
_NVIDIA_VERSION_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("driver_version", ("DRIVER version", "KMD version")),
    ("cuda_driver_version", ("CUDA Version", "CUDA UMD Version")),
    ("nvml_version", ("NVML version",)),
)


def _version_after(text: str, label: str) -> str | None:
    """The numeric version after ``<label> :`` (case-insensitive); a non-numeric value ("Deprecated") is None."""
    match = re.search(re.escape(label) + r"\s*:\s*([0-9]{1,4}(?:\.[0-9]{1,5}){1,3})(?![0-9.])", text, re.IGNORECASE)
    return match.group(1) if match else None


def _first_version(text: str, candidates: tuple[str, ...], used: list[str], *, source: str = "version") -> str | None:
    for label in candidates:
        value = _version_after(text, label)
        if value is not None:
            used.append(f"{source}:{label}")
            return value
    return None


def _optional_int(value: str, low: int, high: int) -> int | None:
    if not re.fullmatch(r"[0-9]{1,9}", value):
        return None
    number = int(value)
    return number if low <= number <= high else None


def _parse_nvidia(output: bytes, fields: tuple[str, ...]) -> tuple[GpuDevice, ...] | None:
    """Validated rows of ``nvidia-smi --query-gpu`` CSV, or None when ANY row does not validate."""
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        return None
    devices: list[GpuDevice] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        values = [item.strip() for item in line.split(",")]
        if len(values) != len(fields):
            return None
        row = dict(zip(fields, values, strict=True))
        index = _optional_int(row["index"], 0, 64)
        name = display_text(row["name"])
        total = _optional_int(row["memory.total"], 1, 1_048_576)
        if index is None or name is None or total is None:
            return None
        driver = row["driver_version"] if _VERSION.fullmatch(row["driver_version"]) else None
        bus = row["pci.bus_id"] if _BUS_ID.fullmatch(row["pci.bus_id"]) else None
        cap = row.get("compute_cap")
        devices.append(GpuDevice(
            index=index, vendor=GpuVendor.NVIDIA, name=name, vram_total_mb=total,
            vram_used_mb=_optional_int(row["memory.used"], 0, 1_048_576),
            vram_free_mb=_optional_int(row["memory.free"], 0, 1_048_576), driver_version=driver,
            compute_capability=cap if cap is not None and _COMPUTE_CAP.fullmatch(cap) else None, pci_bus_id=bus,
            utilization_gpu_pct=_optional_int(row.get("utilization.gpu", ""), 0, 100)))
    return tuple(sorted(devices, key=lambda device: device.index))


def to_hardware_profile(profile: DeviceProfile, *, resource_id: str,
                        node_role: NodeRole = NodeRole.LOCAL_COMPUTE_NODE) -> HardwareProfile:
    """Project a measured profile into the existing ``HardwareProfile`` (fails closed on missing essentials).

    Capacity figures are totals (what the machine has), not momentary free values. A GPU appears only when it
    was measured (``GPU_PRESENT``); CUDA is listed only when ``cuda_available``. Any other GPU state projects to
    a CPU-only profile, so a GPU-requiring model can never "fit" an unverified device.
    """
    if profile.os is None or profile.architecture is None or profile.memory is None:
        raise ValueError("DEVICE_PROFILE_INCOMPLETE")
    cores = profile.cpu.physical_cores or profile.cpu.logical_cores
    if cores is None:
        raise ValueError("DEVICE_PROFILE_INCOMPLETE")
    runtimes = [AcceleratorRuntime.CPU]
    gpu = None
    vram = 0
    if profile.gpu.status is GpuStatus.GPU_PRESENT and profile.gpu.devices:
        device = max(profile.gpu.devices, key=lambda item: (item.vram_total_mb, -item.index))
        gpu = GpuInfo(vendor=device.vendor, label=f"{device.name} {round(device.vram_total_mb / 1024)} GB"[:80])
        vram = device.vram_total_mb
        if profile.gpu.cuda_available:
            runtimes.append(AcceleratorRuntime.CUDA)
    return HardwareProfile(
        resource_id=resource_id, node_role=node_role, os=profile.os, architecture=profile.architecture,
        cpu_cores=cores, ram_mb=profile.memory.total_mb, gpu=gpu, vram_mb=vram,
        runtimes=tuple(sorted(runtimes, key=RUNTIME_ORDER.__getitem__)), machine_ref=profile.machine.machine_ref,
        observed_at=profile.measured_at)
