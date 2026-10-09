"""Inventory: every fixture node against a requirement matrix, staleness, unified memory, determinism."""
from __future__ import annotations

from datetime import timedelta

import pytest
from resource_fixtures import (
    ALL_NODES,
    AS_OF,
    EMBED_SMALL,
    FUTURE_NPU,
    LINUX_CPU,
    MAC_UNIFIED,
    OBSERVED,
    OCR,
    RT,
    TEXT_7B,
    TEXT_70B,
    WINDOWS_NVIDIA,
    hardware,
)

from pados.capability.model import AdapterDescriptor, Determinism, ResourceRequirement
from pados.capability.policy import PolicyCode, PolicyContext, authorize_scope, evaluate
from pados.capability.seams import ResourceInventory, ResourceMatch
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.resources.hardware import GpuInfo, GpuVendor
from pados.resources.inventory import (
    StaticResourceInventory,
    requirement_for,
    resource_fit,
)

INVENTORY = StaticResourceInventory(ALL_NODES, as_of=AS_OF)
WIN, MAC, CPU, NPU = (item.resource_id for item in (WINDOWS_NVIDIA, MAC_UNIFIED, LINUX_CPU, FUTURE_NPU))


def codes(match: ResourceMatch) -> set[str]:
    return {reason.split(":", 1)[0] for reason in match.reasons}


def by_id(requirement: ResourceRequirement, inventory: StaticResourceInventory = INVENTORY) -> dict[str, ResourceMatch]:
    return {item.resource_id: item for item in inventory.match(requirement)}


R = ResourceRequirement
MATRIX: list[tuple[str, ResourceRequirement, dict[str, set[str]]]] = [
    # (name, requirement, {resource_id: failing codes}); absent resource ids fit.
    ("small-cpu", R(cpu_cores=4, ram_mb=8192), {}),
    ("big-ram", R(ram_mb=40_960), {CPU: {"RAM_INSUFFICIENT"}, NPU: {"RAM_INSUFFICIENT"}}),
    ("many-cores", R(cpu_cores=32), {WIN: {"CPU_CORES_INSUFFICIENT"}, MAC: {"CPU_CORES_INSUFFICIENT"},
                                      CPU: {"CPU_CORES_INSUFFICIENT"}, NPU: {"CPU_CORES_INSUFFICIENT"}}),
    ("cuda-12g", R(gpu_required=True, vram_mb=12_288, accelerators=("CUDA",)),
     {MAC: {"ACCELERATOR_UNAVAILABLE"}, CPU: {"GPU_MISSING", "ACCELERATOR_UNAVAILABLE", "VRAM_INSUFFICIENT"},
      NPU: {"GPU_MISSING", "ACCELERATOR_UNAVAILABLE", "VRAM_INSUFFICIENT"}}),
    ("cuda-24g", R(gpu_required=True, vram_mb=24_576, accelerators=("CUDA",)),
     {WIN: {"VRAM_INSUFFICIENT"}, MAC: {"ACCELERATOR_UNAVAILABLE"},
      CPU: {"GPU_MISSING", "ACCELERATOR_UNAVAILABLE", "VRAM_INSUFFICIENT"},
      NPU: {"GPU_MISSING", "ACCELERATOR_UNAVAILABLE", "VRAM_INSUFFICIENT"}}),
    ("metal-48g", R(gpu_required=True, unified_memory_mb=49_152, accelerators=("METAL",)),
     {WIN: {"VRAM_REQUIREMENT_UNDECLARED", "ACCELERATOR_UNAVAILABLE"},
      CPU: {"VRAM_REQUIREMENT_UNDECLARED", "GPU_MISSING", "ACCELERATOR_UNAVAILABLE"},
      NPU: {"VRAM_REQUIREMENT_UNDECLARED", "GPU_MISSING", "ACCELERATOR_UNAVAILABLE"}}),
    ("metal-96g", R(gpu_required=True, unified_memory_mb=98_304, accelerators=("METAL",)),
     {MAC: {"UNIFIED_MEMORY_INSUFFICIENT"},
      WIN: {"VRAM_REQUIREMENT_UNDECLARED", "ACCELERATOR_UNAVAILABLE"},
      CPU: {"VRAM_REQUIREMENT_UNDECLARED", "GPU_MISSING", "ACCELERATOR_UNAVAILABLE"},
      NPU: {"VRAM_REQUIREMENT_UNDECLARED", "GPU_MISSING", "ACCELERATOR_UNAVAILABLE"}}),
    ("any-gpu-8g", R(gpu_required=True, vram_mb=8192, accelerators=("CUDA", "METAL")),
     {CPU: {"GPU_MISSING", "ACCELERATOR_UNAVAILABLE", "VRAM_INSUFFICIENT"},
      NPU: {"GPU_MISSING", "ACCELERATOR_UNAVAILABLE", "VRAM_INSUFFICIENT"}}),
    ("npu", R(ram_mb=4096, accelerators=("NPU",)),
     {WIN: {"ACCELERATOR_UNAVAILABLE"}, MAC: {"ACCELERATOR_UNAVAILABLE"}, CPU: {"ACCELERATOR_UNAVAILABLE"}}),
    ("unknown-accelerator", R(accelerators=("QUANTUM",)),
     {WIN: {"ACCELERATOR_UNKNOWN"}, MAC: {"ACCELERATOR_UNKNOWN"}, CPU: {"ACCELERATOR_UNKNOWN"},
      NPU: {"ACCELERATOR_UNKNOWN"}}),
]
FAILING_PREFIXES = {"TELEMETRY_MARKED_STALE", "TELEMETRY_STALE", "TELEMETRY_FROM_FUTURE", "CPU_CORES_INSUFFICIENT",
                    "RAM_INSUFFICIENT", "VRAM_INSUFFICIENT", "VRAM_REQUIREMENT_UNDECLARED",
                    "UNIFIED_MEMORY_INSUFFICIENT", "GPU_MISSING", "ACCELERATOR_UNAVAILABLE", "ACCELERATOR_UNKNOWN"}


@pytest.mark.parametrize(("name", "requirement", "failures"), MATRIX, ids=[row[0] for row in MATRIX])
def test_fixture_nodes_against_requirement_matrix(name: str, requirement: ResourceRequirement,
                                                  failures: dict[str, set[str]]) -> None:
    matches = by_id(requirement)
    assert set(matches) == {WIN, MAC, CPU, NPU}
    for resource_id, match in matches.items():
        expected = failures.get(resource_id, set())
        assert match.fits is (not expected), (name, resource_id, match.reasons)
        assert codes(match) & FAILING_PREFIXES == expected, (name, resource_id, match.reasons)
        assert "TELEMETRY_FRESH" in codes(match)


def test_every_dimension_is_explained() -> None:
    match = by_id(R(cpu_cores=4, ram_mb=8192, gpu_required=True, vram_mb=4096, accelerators=("CUDA",)))[WIN]
    assert match.fits
    assert match.reasons == ("TELEMETRY_FRESH", "CPU_CORES_OK:required=4:available=16",
                             "RAM_OK:required_mb=8192:available_mb=65536", "VRAM_OK:required_mb=4096:available_mb=16384",
                             "GPU_PRESENT", "ACCELERATOR_AVAILABLE:runtime=CUDA")


def test_unified_memory_is_one_pool() -> None:
    # RAM + VRAM come from the same 64 GB pool: 40 + 30 GB does not fit even though each alone would.
    combined = by_id(R(ram_mb=40_960, gpu_required=True, vram_mb=30_720))[MAC]
    assert not combined.fits
    assert "UNIFIED_MEMORY_INSUFFICIENT:required_mb=71680:available_mb=65536:basis=ram_plus_vram" in combined.reasons
    # The same requirement on a dedicated machine checks RAM and VRAM separately.
    big_gpu = hardware(resource_id="dedicated-big", ram_mb=65_536, gpu=GpuInfo(vendor=GpuVendor.NVIDIA, label="x"),
                       vram_mb=32_768, runtimes=(RT.CPU, RT.CUDA))
    assert by_id(R(ram_mb=40_960, gpu_required=True, vram_mb=30_720),
                 StaticResourceInventory([big_gpu], as_of=AS_OF))["dedicated-big"].fits
    # A declared unified requirement is used as-is, and RAM must still fit the pool.
    declared = by_id(R(ram_mb=8192, gpu_required=True, unified_memory_mb=49_152))[MAC]
    assert declared.fits and any(r.endswith("basis=declared") for r in declared.reasons)
    assert not by_id(R(ram_mb=70_000, unified_memory_mb=1024))[MAC].fits


def test_unified_only_requirement_never_fits_dedicated_vram() -> None:
    match = by_id(R(gpu_required=True, unified_memory_mb=4096))[WIN]
    assert not match.fits and "VRAM_REQUIREMENT_UNDECLARED:unified_required_mb=4096" in match.reasons


@pytest.mark.parametrize("profile", [
    hardware(resource_id="marked-stale", stale=True),
    hardware(resource_id="old-telemetry", observed_at=OBSERVED - timedelta(hours=2)),
    hardware(resource_id="future-telemetry", observed_at=AS_OF + timedelta(hours=1)),
])
def test_stale_telemetry_never_fits(profile: object) -> None:
    inventory = StaticResourceInventory([profile], as_of=AS_OF)  # type: ignore[list-item]
    for requirement in (R(cpu_cores=0), R(), R(ram_mb=1)):
        (match,) = inventory.match(requirement)
        assert not match.fits
        assert codes(match) & {"TELEMETRY_MARKED_STALE", "TELEMETRY_STALE", "TELEMETRY_FROM_FUTURE"}
    (feasible,) = [inventory.feasibility(EMBED_SMALL, inventory.resource_ids[0])]
    assert not feasible.fits


def test_staleness_bounds_are_configurable_and_validated() -> None:
    old = hardware(resource_id="old-telemetry", observed_at=OBSERVED - timedelta(hours=2))
    lenient = StaticResourceInventory([old], as_of=AS_OF, max_telemetry_age=timedelta(hours=3))
    assert lenient.match(R())[0].fits
    stale_reason = StaticResourceInventory([old], as_of=AS_OF).match(R())[0].reasons[0]
    assert stale_reason == "TELEMETRY_STALE:age_s=7500:max_s=900"
    with pytest.raises(ValueError, match="INVENTORY_TELEMETRY_BOUNDS_INVALID"):
        StaticResourceInventory([old], as_of=AS_OF, max_telemetry_age=timedelta(0))
    with pytest.raises(ValueError, match="INVENTORY_AS_OF_NOT_AWARE"):
        StaticResourceInventory([old], as_of=AS_OF.replace(tzinfo=None))


def test_inventory_rejects_duplicates_and_foreign_objects() -> None:
    with pytest.raises(ValueError, match="INVENTORY_RESOURCE_DUPLICATE"):
        StaticResourceInventory([LINUX_CPU, LINUX_CPU], as_of=AS_OF)
    with pytest.raises(TypeError, match="HARDWARE_PROFILE_TYPE_INVALID"):
        StaticResourceInventory([LINUX_CPU.model_dump()], as_of=AS_OF)  # type: ignore[list-item]
    with pytest.raises(KeyError, match="RESOURCE_NOT_IN_INVENTORY"):
        INVENTORY.feasibility(TEXT_7B, "missing-node")


def test_match_is_deterministic_and_sorted() -> None:
    requirement = R(ram_mb=40_960)
    shuffled = StaticResourceInventory(reversed(ALL_NODES), as_of=AS_OF)
    assert INVENTORY.match(requirement) == INVENTORY.match(requirement) == shuffled.match(requirement)
    assert [item.resource_id for item in INVENTORY.match(requirement)] == sorted([WIN, MAC, CPU, NPU])


def test_inventory_implements_the_fabric_seam() -> None:
    seam: ResourceInventory = INVENTORY
    assert all(isinstance(item, ResourceMatch) for item in seam.match(R()))


def test_requirement_for_uses_declared_figures_per_runtime() -> None:
    assert requirement_for(TEXT_7B, RT.CPU) == R(cpu_cores=1, ram_mb=8192, accelerators=("CPU",))
    assert requirement_for(TEXT_7B, RT.CUDA) == R(cpu_cores=1, gpu_required=True, vram_mb=6144,
                                                  unified_memory_mb=7168, accelerators=("CUDA",))
    assert requirement_for(TEXT_7B, RT.METAL) == R(cpu_cores=1, gpu_required=True, unified_memory_mb=7168,
                                                   accelerators=("METAL",))
    with pytest.raises(ValueError, match="MODEL_RUNTIME_NOT_DECLARED"):
        requirement_for(TEXT_7B, RT.ROCM)


def test_model_feasibility_per_node_with_headroom() -> None:
    win = INVENTORY.feasibility(TEXT_7B, WIN)
    assert win.fitting_runtimes == (RT.CPU, RT.CUDA)
    assert {o.runtime: o.headroom_mb for o in win.options} == {RT.CPU: 57_344, RT.CUDA: 10_240, RT.METAL: None}
    assert win.headroom_mb == 57_344
    assert INVENTORY.feasibility(TEXT_7B, MAC).fitting_runtimes == (RT.CPU, RT.METAL)
    assert INVENTORY.feasibility(TEXT_7B, CPU).fitting_runtimes == (RT.CPU,)
    big_win = INVENTORY.feasibility(TEXT_70B, WIN)
    assert big_win.fitting_runtimes == (RT.CPU,)  # 40 GB VRAM does not fit a 16 GB card
    cuda = next(o for o in big_win.options if o.runtime is RT.CUDA)
    assert "VRAM_INSUFFICIENT:required_mb=40960:available_mb=16384" in cuda.reasons
    big_mac = INVENTORY.feasibility(TEXT_70B, MAC)
    assert big_mac.fitting_runtimes == (RT.CPU, RT.METAL)
    assert {o.runtime: o.headroom_mb for o in big_mac.options}[RT.METAL] == 65_536 - 45_056
    assert not INVENTORY.feasibility(TEXT_70B, CPU).fits
    assert INVENTORY.feasibility(EMBED_SMALL, NPU).fitting_runtimes == (RT.CPU, RT.NPU)


def test_resource_fit_feeds_policy_without_new_authority() -> None:
    contract = {"work_contract_id": "wc-1", "project_id": "p", "required_capabilities": ["CAPABILITY:DOCUMENT_OCR"]}
    grant = {"work_contract_id": "wc-1", "project_id": "p", "capabilities": ["CAPABILITY:DOCUMENT_OCR"]}
    adapter = AdapterDescriptor(adapter_id="ocr.text-layer", capability_id="DOCUMENT_OCR", version="1",
                                backend="pypdf", determinism=Determinism.DETERMINISTIC,
                                max_classification=DataClassification.RESTRICTED,
                                network_requirement=NetworkRequirement.LOCAL_ONLY)
    matches = INVENTORY.match(OCR.resources)
    for resource_id, expected in ((CPU, PolicyCode.ALLOWED), ("missing-node", PolicyCode.RESOURCE_UNAVAILABLE)):
        context = PolicyContext(scope=authorize_scope(contract, grant), project_id="p", stage_id="s1",
                                classification=DataClassification.INTERNAL,
                                network_policy=NetworkRequirement.LOCAL_ONLY, resource_id=resource_id)
        decision = evaluate(OCR, adapter, context, adapter_available=lambda: True,
                            resource_fit=resource_fit(matches, resource_id))
        assert decision.code is expected
    stale = StaticResourceInventory([hardware(stale=True)], as_of=AS_OF)
    assert not resource_fit(stale.match(OCR.resources), LINUX_CPU.resource_id)
    duplicated = (*matches, *matches)
    assert not resource_fit(duplicated, CPU)


def test_cr10_r01_any_future_dated_telemetry_never_fits() -> None:
    from pados.resources.inventory import FitCode, freshness

    for ahead in (timedelta(seconds=1), timedelta(minutes=1), timedelta(minutes=2)):
        node = LINUX_CPU.model_copy(update={"observed_at": AS_OF + ahead})
        reason = freshness(node, as_of=AS_OF, max_age=timedelta(minutes=15), max_future_skew=timedelta(minutes=2))
        assert reason.startswith(FitCode.TELEMETRY_FROM_FUTURE.value)


def test_cr10_r02_unified_pool_must_cover_combined_ram_and_vram_demand() -> None:
    from pados.resources.inventory import FitCode, assess, freshness

    telemetry = freshness(MAC_UNIFIED, as_of=AS_OF, max_age=timedelta(minutes=15), max_future_skew=timedelta(0))
    pool = MAC_UNIFIED.unified_memory_mb
    # a small declared unified figure cannot undercut a large capability VRAM minimum
    requirement = ResourceRequirement(ram_mb=0, vram_mb=pool + 1, unified_memory_mb=1024, gpu_required=True)
    result = assess(requirement, MAC_UNIFIED, telemetry=telemetry)
    assert any(r.startswith(FitCode.UNIFIED_MEMORY_INSUFFICIENT.value) for r in result.reasons)
    assert not result.fits
