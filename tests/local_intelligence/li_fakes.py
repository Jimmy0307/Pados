"""Deterministic host and command fakes for the Device Profiler.

The Windows/RTX fixture has the SHAPE of real ``nvidia-smi`` output; its figures are test data, not a
measurement of any machine (live facts come only from a captured Windows run).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pados.local_intelligence.probes import CommandNotFound, CommandResult, CommandTimedOut, ProbeSession

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
DISPLAY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"

RTX_QUERY = b"0, NVIDIA GeForce RTX 5070 Laptop GPU, 8151, 0, 7820, 581.29, 00000000:01:00.0, 12.0, 0\r\n"
RTX_VERSION = (b"NVIDIA-SMI version  : 581.29\r\nNVML version        : 581.29\r\nDRIVER version      : 581.29\r\n"
               b"CUDA Version        : 13.0\r\n")

# Byte-exact outputs of driver 616.92 as captured on the Windows RTX 5070 target (Stage B M0 run
# m0-20261007T085612Z, capture/raw/*.out): "DRIVER version" and "CUDA version" are deprecated there.
RTX_616_QUERY = b"0, NVIDIA GeForce RTX 5070 Laptop GPU, 8151, 0, 7891, 616.92, 00000000:01:00.0, 12.0, 0\r\n"
RTX_616_VERSION = (b"NVIDIA-SMI version  : 616.92\r\nNVML version        : 616.92\r\n"
                   b"DRIVER version      : Deprecated, see \"KMD version\" instead\r\n"
                   b"CUDA version        : Deprecated, see \"CUDA UMD version\" instead\r\n"
                   b"KMD version         : 616.92\r\nCUDA UMD version    : 13.4\r\n")
RTX_616_SUMMARY_HEADER = (b"+------------------------------------------+\r\n"
                          b"| NVIDIA-SMI 616.92                 KMD Version: 616.92        CUDA UMD Version: 13.4     |\r\n")


@dataclass
class FakeHost:
    system: str = "Linux"
    release_value: str = "6.8.0"
    version_value: str = "#1 SMP"
    machine_value: str = "x86_64"
    host: str = "Secret-Host-Name"
    files: dict[str, str] = field(default_factory=dict)
    dirs: dict[str, list[str]] = field(default_factory=dict)
    registry: dict[tuple[str, str], object] = field(default_factory=dict)
    subkeys: dict[str, list[str]] = field(default_factory=dict)
    counts: tuple[int | None, int | None] = (8, 16)
    mem: tuple[int, int] | None = (32 * 2**30, 20 * 2**30)
    disk_figures: tuple[int, int] | None = (1000 * 2**30, 400 * 2**30)
    battery: bool | None = None
    address: str | None = "192.168.1.20"

    def release(self) -> str:
        return self.release_value

    def version(self) -> str:
        return self.version_value

    def machine(self) -> str:
        return self.machine_value

    def hostname(self) -> str:
        return self.host

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)

    def list_dir(self, path: str) -> list[str] | None:
        return self.dirs.get(path)

    def cpu_counts(self) -> tuple[int | None, int | None]:
        return self.counts

    def memory(self) -> tuple[int, int] | None:
        return self.mem

    def disk(self, path: str) -> tuple[int, int] | None:
        return self.disk_figures

    def battery_present(self) -> bool | None:
        return self.battery

    def registry_value(self, key: str, name: str) -> object:
        return self.registry.get((key, name))

    def registry_subkeys(self, key: str) -> list[str] | None:
        return self.subkeys.get(key)

    def outbound_address(self) -> str | None:
        return self.address


Reply = CommandResult | type[Exception]


@dataclass
class FakeRunner:
    """Maps an argv (joined by spaces) to a result; unknown commands are 'not found'."""

    replies: dict[str, Reply] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], timeout: float) -> CommandResult:
        self.calls.append(tuple(argv))
        reply = self.replies.get(" ".join(argv))
        if reply is None:
            raise CommandNotFound(argv[0])
        if isinstance(reply, type):
            raise reply(argv[0])
        return reply


def nvidia_query(fields: str = "index,name,memory.total,memory.used,memory.free,driver_version,pci.bus_id,"
                 "compute_cap,utilization.gpu") -> str:
    return f"nvidia-smi --query-gpu={fields} --format=csv,noheader,nounits"


BASIC_QUERY = nvidia_query("index,name,memory.total,memory.used,memory.free,driver_version,pci.bus_id")


def windows_rtx_host(*, battery: bool = True) -> FakeHost:
    nt = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    return FakeHost(
        system="Windows", release_value="11", version_value="10.0.26100", machine_value="AMD64", battery=battery,
        counts=(24, 32), mem=(32 * 2**30, 22 * 2**30),
        registry={
            (nt, "CurrentBuildNumber"): "26100", (nt, "DisplayVersion"): "24H2", (nt, "EditionID"): "Professional",
            (r"HARDWARE\DESCRIPTION\System\BIOS", "SystemManufacturer"): "Laptop Vendor Inc.",
            (r"HARDWARE\DESCRIPTION\System\BIOS", "SystemProductName"): "Gaming Laptop 16",
            (r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString"): "Intel(R) Core(TM) i9-14900HX",
            (DISPLAY + "\\0000", "DriverDesc"): "Intel(R) UHD Graphics",
            (DISPLAY + "\\0000", "MatchingDeviceId"): "PCI\\VEN_8086&DEV_A788",
            (DISPLAY + "\\0000", "DriverVersion"): "31.0.101.5592",
            (DISPLAY + "\\0001", "DriverDesc"): "NVIDIA GeForce RTX 5070 Laptop GPU",
            (DISPLAY + "\\0001", "MatchingDeviceId"): "PCI\\VEN_10DE&DEV_2D58",
            (DISPLAY + "\\0001", "DriverVersion"): "32.0.15.8129",
        },
        subkeys={DISPLAY: ["0000", "0001", "Configuration", "Properties"]})


def windows_rtx_runner() -> FakeRunner:
    return FakeRunner({nvidia_query(): CommandResult(0, RTX_QUERY), "nvidia-smi --version": CommandResult(0, RTX_VERSION),
                       "nvcc --version": CommandResult(0, b"Cuda compilation tools, release 12.8, V12.8.93\n")})


def vmware_host() -> FakeHost:
    root = "/sys/bus/pci/devices"
    return FakeHost(
        counts=(4, 4), mem=(8 * 2**30, 6 * 2**30), address="192.168.1.21",
        files={"/etc/os-release": 'PRETTY_NAME="Ubuntu 24.04.5 LTS"\n', "/sys/class/dmi/id/sys_vendor": "VMware, Inc.\n",
               "/sys/class/dmi/id/product_name": "VMware Virtual Platform\n",
               "/proc/cpuinfo": "model name\t: Intel(R) Core(TM) i9-14900HX\n",
               f"{root}/0000:00:0f.0/class": "0x030000\n", f"{root}/0000:00:0f.0/vendor": "0x15ad\n",
               f"{root}/0000:00:0f.0/device": "0x0405\n", f"{root}/0000:00:10.0/class": "0x010700\n"},
        dirs={root: ["0000:00:0f.0", "0000:00:10.0"]})


def session(runner: FakeRunner) -> ProbeSession:
    ticks = iter(range(10_000))
    return ProbeSession(runner=runner, clock=lambda: next(ticks) / 1000)


__all__ = ["BASIC_QUERY", "DISPLAY", "NOW", "RTX_616_QUERY", "RTX_616_SUMMARY_HEADER", "RTX_616_VERSION", "RTX_QUERY", "CommandResult", "CommandTimedOut", "FakeHost", "FakeRunner",
           "nvidia_query", "session", "vmware_host", "windows_rtx_host", "windows_rtx_runner"]
