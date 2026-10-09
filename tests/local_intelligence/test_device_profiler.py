import json

import pytest
from li_fakes import (
    BASIC_QUERY,
    DISPLAY,
    NOW,
    RTX_616_QUERY,
    RTX_616_SUMMARY_HEADER,
    RTX_616_VERSION,
    CommandResult,
    CommandTimedOut,
    FakeRunner,
    nvidia_query,
    session,
    vmware_host,
    windows_rtx_host,
    windows_rtx_runner,
)

from pados.local_intelligence.device import (
    ComputeClass,
    DeviceProfiler,
    FormFactor,
    GpuStatus,
    NetworkMode,
    to_hardware_profile,
)
from pados.local_intelligence.probes import Basis, ProbeOutcome
from pados.resources.hardware import AcceleratorRuntime, GpuVendor, OperatingSystem
from pados.resources.inventory import StaticResourceInventory


def _profile(host, runner, **kwargs):
    return DeviceProfiler(host=host, session=session(runner), clock=lambda: NOW, **kwargs).profile()


def test_windows_rtx_laptop_profile_is_measured_and_projects_cuda():
    profile = _profile(windows_rtx_host(), windows_rtx_runner())
    assert profile.os is OperatingSystem.WINDOWS and profile.os_name == "Windows 11 Professional 24H2 build 26100"
    assert profile.basis is Basis.PADOS_MEASURED and profile.cpu.model == "Intel(R) Core(TM) i9-14900HX"
    assert (profile.cpu.physical_cores, profile.cpu.logical_cores) == (24, 32)
    gpu = profile.gpu
    assert gpu.status is GpuStatus.GPU_PRESENT and gpu.cuda_available
    device = gpu.devices[0]
    assert (device.name, device.vram_total_mb, device.vram_free_mb, device.compute_capability, device.driver_version) == (
        "NVIDIA GeForce RTX 5070 Laptop GPU", 8151, 7820, "12.0", "581.29")
    assert (gpu.nvidia.cuda_driver_version, gpu.nvidia.driver_version, gpu.cuda_toolkit.version) == ("13.0", "581.29", "12.8")
    assert {a.vendor for a in gpu.adapters} == {GpuVendor.INTEL, GpuVendor.NVIDIA}
    assert profile.machine.form_factor is FormFactor.LAPTOP and profile.machine_class == "LAPTOP/DISCRETE_GPU"
    hardware = to_hardware_profile(profile, resource_id="win-rtx-laptop")
    assert hardware.runtimes == (AcceleratorRuntime.CPU, AcceleratorRuntime.CUDA) and hardware.vram_mb == 8151
    assert hardware.gpu.label == "NVIDIA GeForce RTX 5070 Laptop GPU 8 GB" and hardware.ram_mb == 32 * 1024
    inventory = StaticResourceInventory([hardware], as_of=NOW)
    assert inventory.hardware("win-rtx-laptop").observed_at == NOW


def test_vmware_degrades_gracefully_to_a_cpu_only_profile():
    runner = FakeRunner()
    profile = _profile(vmware_host(), runner)
    assert profile.gpu.status is GpuStatus.GPU_ABSENT and profile.gpu.code == "NO_COMPUTE_GPU_ENUMERATED"
    assert profile.gpu.adapters[0].virtual and profile.gpu.adapters[0].vendor_id == "15ad"
    assert profile.machine.form_factor is FormFactor.VIRTUAL_MACHINE and profile.machine.virtualization == "VMWARE"
    assert profile.compute_class is ComputeClass.NO_USABLE_GPU and profile.network_mode is NetworkMode.PRIVATE_NETWORK
    assert not profile.gpu.cuda_available and not profile.gpu.nvidia.smi_available
    outcomes = {r.probe_id: r.outcome for r in profile.probes}
    assert outcomes["nvidia-smi.query-gpu"] is ProbeOutcome.NOT_FOUND
    hardware = to_hardware_profile(profile, resource_id="vmware-dev")
    assert hardware.runtimes == (AcceleratorRuntime.CPU,) and hardware.gpu is None and hardware.vram_mb == 0
    assert runner.calls == [tuple(nvidia_query().split(" "))]  # nvcc is never probed without a GPU


def test_nvidia_adapter_without_nvidia_smi_is_unavailable_not_absent():
    profile = _profile(windows_rtx_host(), FakeRunner())
    assert profile.gpu.status is GpuStatus.GPU_UNAVAILABLE and profile.gpu.code == "NVIDIA_SMI_NOT_FOUND"
    assert to_hardware_profile(profile, resource_id="win-test").runtimes == (AcceleratorRuntime.CPU,)


def test_driver_failure_is_unavailable_after_the_basic_retry():
    failing = CommandResult(9, b"NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\n")
    runner = FakeRunner({nvidia_query(): failing, BASIC_QUERY: failing, "nvidia-smi --version": failing,
                         "nvidia-smi": failing})
    profile = _profile(windows_rtx_host(), runner)
    assert profile.gpu.status is GpuStatus.GPU_UNAVAILABLE and profile.gpu.code == "NVIDIA_DRIVER_UNAVAILABLE"
    assert [r.outcome for r in profile.probes if r.probe_id.startswith("nvidia-smi.query")] == [ProbeOutcome.FAILED] * 2


def test_older_driver_without_compute_cap_uses_the_basic_query():
    basic = b"0, NVIDIA GeForce RTX 5070 Laptop GPU, 8151, 10, 8000, 581.29, 00000000:01:00.0\n"
    runner = FakeRunner({nvidia_query(): CommandResult(6, b'Field "compute_cap" is not a valid field\n'),
                         BASIC_QUERY: CommandResult(0, basic)})
    profile = _profile(windows_rtx_host(), runner)
    assert profile.gpu.status is GpuStatus.GPU_PRESENT and profile.gpu.devices[0].compute_capability is None
    assert not profile.gpu.cuda_available  # no CUDA version reported by the driver -> not claimed
    assert to_hardware_profile(profile, resource_id="win-test").runtimes == (AcceleratorRuntime.CPU,)


@pytest.mark.parametrize("payload", [
    b"0, NVIDIA GeForce RTX 5070; rm -rf /, 8151, 0, 8151, 581.29, 00000000:01:00.0, 12.0, 0\n",
    b"0, NVIDIA GeForce RTX 5070 Laptop GPU, lots, 0, 8151, 581.29, 00000000:01:00.0, 12.0, 0\n",
    b"0, NVIDIA GeForce RTX 5070 Laptop GPU, 8151\n",
    b"\xff\xfe garbage",
])
def test_untrusted_tool_output_that_does_not_validate_is_never_a_gpu(payload):
    runner = FakeRunner({nvidia_query(): CommandResult(0, payload)})
    profile = _profile(windows_rtx_host(), runner)
    assert profile.gpu.status is GpuStatus.GPU_DISCOVERY_FAILED and profile.gpu.devices == ()
    record = next(r for r in profile.probes if r.probe_id == "nvidia-smi.query-gpu")
    assert record.outcome is ProbeOutcome.INVALID_OUTPUT and record.output_sha256 is not None
    assert to_hardware_profile(profile, resource_id="win-test").gpu is None


def test_enumeration_failure_without_vendor_tool_is_discovery_failed():
    host = vmware_host()
    host.dirs = {}
    profile = _profile(host, FakeRunner())
    assert profile.gpu.status is GpuStatus.GPU_DISCOVERY_FAILED
    assert profile.gpu.code == "PLATFORM_ENUMERATION_FAILED;NVIDIA_SMI_NOT_FOUND"
    assert profile.compute_class is ComputeClass.GPU_UNVERIFIED


def test_timeout_is_recorded_and_classified():
    runner = FakeRunner({nvidia_query(): CommandTimedOut})
    profile = _profile(windows_rtx_host(), runner)
    assert profile.gpu.status is GpuStatus.GPU_DISCOVERY_FAILED and profile.gpu.code == "NVIDIA_SMI_TIMED_OUT"
    assert profile.probes[1].outcome is ProbeOutcome.TIMED_OUT


def test_amd_or_intel_gpu_is_reported_without_claiming_a_runtime():
    host = vmware_host()
    host.files["/sys/bus/pci/devices/0000:00:0f.0/vendor"] = "0x1002\n"
    profile = _profile(host, FakeRunner())
    assert profile.gpu.status is GpuStatus.GPU_UNAVAILABLE and profile.gpu.code == "GPU_RUNTIME_PROBE_NOT_IMPLEMENTED:AMD"


def test_profile_never_records_hostname_or_paths():
    profile = _profile(windows_rtx_host(), windows_rtx_runner(), disk_path="C:/Users/someone/pados")
    text = json.dumps(profile.evidence())
    assert "Secret-Host-Name" not in text and "someone" not in text
    assert profile.machine.hostname_sha256 == profile.machine.hostname_sha256.lower() and len(text) < 20_000


def test_profile_digest_is_deterministic_and_content_bound():
    first = _profile(windows_rtx_host(), windows_rtx_runner())
    second = _profile(windows_rtx_host(), windows_rtx_runner())
    assert first.profile_digest == second.profile_digest
    third = _profile(windows_rtx_host(battery=False), windows_rtx_runner())
    assert third.profile_digest != first.profile_digest and third.machine.form_factor is FormFactor.NO_BATTERY_REPORTED


def test_projection_fails_closed_on_missing_essentials():
    host = vmware_host()
    host.mem = None
    with pytest.raises(ValueError, match="DEVICE_PROFILE_INCOMPLETE"):
        to_hardware_profile(_profile(host, FakeRunner()), resource_id="vmware-dev")


def test_network_modes():
    for address, mode in ((None, NetworkMode.NO_ROUTE), ("127.0.0.1", NetworkMode.LOOPBACK_ONLY),
                          ("10.1.2.3", NetworkMode.PRIVATE_NETWORK), ("8.8.4.4", NetworkMode.PUBLIC_ADDRESS)):
        host = vmware_host()
        host.address = address
        assert _profile(host, FakeRunner()).network_mode is mode


def test_driver_616_kmd_and_cuda_umd_labels_are_read():
    """Replay of the machine-captured Windows outputs: deprecated labels must not hide the real versions."""
    runner = FakeRunner({nvidia_query(): CommandResult(0, RTX_616_QUERY), "nvidia-smi --version": CommandResult(0, RTX_616_VERSION)})
    profile = _profile(windows_rtx_host(), runner)
    nvidia = profile.gpu.nvidia
    assert (nvidia.driver_version, nvidia.cuda_driver_version, nvidia.nvml_version) == ("616.92", "13.4", "616.92")
    assert nvidia.version_labels == ("version:KMD version", "version:CUDA UMD Version", "version:NVML version")
    assert profile.gpu.cuda_available and profile.machine_class == "LAPTOP/DISCRETE_GPU"
    hardware = to_hardware_profile(profile, resource_id="win-rtx-laptop")
    assert hardware.runtimes == (AcceleratorRuntime.CPU, AcceleratorRuntime.CUDA) and hardware.vram_mb == 8151
    assert "nvidia-smi.summary" not in {r.probe_id for r in profile.probes}


def test_summary_header_fallback_reads_cuda_umd_version():
    version = b"NVIDIA-SMI version  : 616.92\r\nDRIVER version      : Deprecated\r\n"
    runner = FakeRunner({nvidia_query(): CommandResult(0, RTX_616_QUERY), "nvidia-smi --version": CommandResult(0, version),
                         "nvidia-smi": CommandResult(0, RTX_616_SUMMARY_HEADER)})
    nvidia = _profile(windows_rtx_host(), runner).gpu.nvidia
    assert (nvidia.driver_version, nvidia.cuda_driver_version) == ("616.92", "13.4")
    assert nvidia.version_labels == ("summary:KMD version", "summary:CUDA UMD Version")


def test_deprecated_values_alone_never_claim_cuda():
    version = b"DRIVER version : Deprecated, see KMD version\r\nCUDA version : Deprecated, see CUDA UMD version\r\n"
    runner = FakeRunner({nvidia_query(): CommandResult(0, RTX_616_QUERY), "nvidia-smi --version": CommandResult(0, version),
                         "nvidia-smi": CommandResult(0, b"| NVIDIA-SMI 616.92 |\r\n")})
    profile = _profile(windows_rtx_host(), runner)
    assert profile.gpu.status is GpuStatus.GPU_PRESENT and profile.gpu.nvidia.cuda_driver_version is None
    assert not profile.gpu.cuda_available
    assert to_hardware_profile(profile, resource_id="win-test").runtimes == (AcceleratorRuntime.CPU,)


def test_lower_case_inf_vendor_ids_identify_the_nvidia_adapter():
    host = windows_rtx_host()
    host.registry[(DISPLAY + "\\0001", "MatchingDeviceId")] = "pci\\ven_10de&dev_2d58&subsys_00000000"
    profile = _profile(host, FakeRunner())  # no nvidia-smi on PATH
    assert {a.vendor for a in profile.gpu.adapters} == {GpuVendor.INTEL, GpuVendor.NVIDIA}
    assert profile.gpu.status is GpuStatus.GPU_UNAVAILABLE and profile.gpu.code == "NVIDIA_SMI_NOT_FOUND"


def test_an_unidentified_adapter_is_not_evidence_of_absence():
    host = windows_rtx_host()
    del host.registry[(DISPLAY + "\\0001", "MatchingDeviceId")]
    host.registry[(DISPLAY + "\\0000", "MatchingDeviceId")] = "PCI\\VEN_1414&DEV_008C"  # Microsoft Basic Display
    profile = _profile(host, FakeRunner())
    assert profile.gpu.status is GpuStatus.GPU_UNAVAILABLE
    assert profile.gpu.code == "GPU_VENDOR_UNIDENTIFIED;NVIDIA_SMI_NOT_FOUND"
