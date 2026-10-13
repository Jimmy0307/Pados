# SPDX-License-Identifier: AGPL-3.0-only
"""Resource inventory: deterministic, explained fit of requirements to declared hardware.

Implements ``pados.capability.seams.ResourceInventory``. Every evaluation is a pure function of the
requirement, the declared profiles and the inventory's fixed ``as_of`` instant (no clock reads, no probes).
A fit is a description, never an authorization: it only feeds ``policy.evaluate(resource_fit=...)``.

Memory semantics (fail closed):

* dedicated machine - RAM and VRAM are separate: ``ram_mb <= ram`` and ``vram_mb <= vram``. A requirement
  sized only for unified memory (``unified_memory_mb`` without ``vram_mb``) cannot be converted into a VRAM
  figure, so it does not fit (``VRAM_REQUIREMENT_UNDECLARED``).
* unified-memory machine (e.g. Apple silicon) - RAM and GPU memory are one pool reported once. The pool
  demand is the declared ``unified_memory_mb`` or, when undeclared, the conservative sum ``ram + vram``;
  ``ram_mb`` must fit the pool as well.
* ``accelerators`` is an any-of set: at least one listed runtime must be available; unknown names fail.
* stale telemetry (flagged, older than ``max_telemetry_age`` or from the future) never fits.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pados.capability.model import ResourceRequirement
from pados.capability.seams import ResourceMatch
from pados.resources.hardware import (
    GPU_RUNTIMES,
    RUNTIME_ORDER,
    AcceleratorRuntime,
    HardwareProfile,
    aware,
)
from pados.resources.models import ModelRuntimeProfile

MAX_RESOURCES = 256
DEFAULT_MAX_TELEMETRY_AGE = timedelta(minutes=15)
DEFAULT_MAX_FUTURE_SKEW = timedelta(minutes=2)


class FitCode(StrEnum):
    TELEMETRY_FRESH = "TELEMETRY_FRESH"
    TELEMETRY_MARKED_STALE = "TELEMETRY_MARKED_STALE"
    TELEMETRY_STALE = "TELEMETRY_STALE"
    TELEMETRY_FROM_FUTURE = "TELEMETRY_FROM_FUTURE"
    CPU_CORES_OK = "CPU_CORES_OK"
    CPU_CORES_INSUFFICIENT = "CPU_CORES_INSUFFICIENT"
    RAM_OK = "RAM_OK"
    RAM_INSUFFICIENT = "RAM_INSUFFICIENT"
    VRAM_OK = "VRAM_OK"
    VRAM_INSUFFICIENT = "VRAM_INSUFFICIENT"
    VRAM_NOT_REQUIRED = "VRAM_NOT_REQUIRED"
    VRAM_REQUIREMENT_UNDECLARED = "VRAM_REQUIREMENT_UNDECLARED"
    UNIFIED_MEMORY_OK = "UNIFIED_MEMORY_OK"
    UNIFIED_MEMORY_INSUFFICIENT = "UNIFIED_MEMORY_INSUFFICIENT"
    GPU_PRESENT = "GPU_PRESENT"
    GPU_MISSING = "GPU_MISSING"
    GPU_NOT_REQUIRED = "GPU_NOT_REQUIRED"
    ACCELERATOR_AVAILABLE = "ACCELERATOR_AVAILABLE"
    ACCELERATOR_UNAVAILABLE = "ACCELERATOR_UNAVAILABLE"
    ACCELERATOR_UNKNOWN = "ACCELERATOR_UNKNOWN"
    ACCELERATOR_NOT_REQUIRED = "ACCELERATOR_NOT_REQUIRED"
    RESOURCE_NOT_IN_INVENTORY = "RESOURCE_NOT_IN_INVENTORY"


FAILING = frozenset({
    FitCode.TELEMETRY_MARKED_STALE, FitCode.TELEMETRY_STALE, FitCode.TELEMETRY_FROM_FUTURE,
    FitCode.CPU_CORES_INSUFFICIENT, FitCode.RAM_INSUFFICIENT, FitCode.VRAM_INSUFFICIENT,
    FitCode.VRAM_REQUIREMENT_UNDECLARED, FitCode.UNIFIED_MEMORY_INSUFFICIENT, FitCode.GPU_MISSING,
    FitCode.ACCELERATOR_UNAVAILABLE, FitCode.ACCELERATOR_UNKNOWN, FitCode.RESOURCE_NOT_IN_INVENTORY,
})


@dataclass(frozen=True)
class Assessment:
    """One requirement against one resource: every dimension explained, plus memory headroom."""

    resource_id: str
    fits: bool
    reasons: tuple[str, ...]
    headroom_mb: int | None  # smallest (available - required) over constrained memory dimensions

    def as_match(self) -> ResourceMatch:
        return ResourceMatch(resource_id=self.resource_id, fits=self.fits, reasons=self.reasons)


def _reason(code: FitCode, **detail: object) -> str:
    return ":".join((code.value, *(f"{key}={value}" for key, value in detail.items())))


def freshness(hardware: HardwareProfile, *, as_of: datetime, max_age: timedelta,
              max_future_skew: timedelta) -> str:
    if hardware.stale:
        return _reason(FitCode.TELEMETRY_MARKED_STALE)
    age = as_of - hardware.observed_at
    del max_future_skew  # kept for API stability; future-dated telemetry never fits (Codex CR-10 R-01)
    if age < timedelta(0):
        return _reason(FitCode.TELEMETRY_FROM_FUTURE)
    if age > max_age:
        return _reason(FitCode.TELEMETRY_STALE, age_s=int(age.total_seconds()), max_s=int(max_age.total_seconds()))
    return _reason(FitCode.TELEMETRY_FRESH)


def assess(requirement: ResourceRequirement, hardware: HardwareProfile, *, telemetry: str) -> Assessment:
    """Pure evaluation of one requirement on one hardware profile (``telemetry`` from ``freshness``)."""
    reasons = [telemetry]
    headroom: list[int] = []

    def capacity(ok: FitCode, short: FitCode, required: int, available: int, unit: str) -> None:
        reasons.append(_reason(ok if available >= required else short,
                               **{f"required{unit}": required, f"available{unit}": available}))
        if unit == "_mb" and required:
            headroom.append(available - required)

    capacity(FitCode.CPU_CORES_OK, FitCode.CPU_CORES_INSUFFICIENT, requirement.cpu_cores, hardware.cpu_cores, "")
    capacity(FitCode.RAM_OK, FitCode.RAM_INSUFFICIENT, requirement.ram_mb, hardware.ram_mb, "_mb")
    if hardware.unified:
        # One pool: the declared unified figure can never undercut the combined RAM + VRAM demand of the
        # capability or model path (Codex CR-10 R-02); the larger of the two must fit.
        demand = max(requirement.unified_memory_mb, requirement.ram_mb + requirement.vram_mb)
        code = FitCode.UNIFIED_MEMORY_OK if hardware.unified_memory_mb >= demand else FitCode.UNIFIED_MEMORY_INSUFFICIENT
        reasons.append(_reason(code, required_mb=demand, available_mb=hardware.unified_memory_mb,
                               basis="declared" if requirement.unified_memory_mb >= requirement.ram_mb + requirement.vram_mb else "ram_plus_vram"))
        if demand:
            headroom.append(hardware.unified_memory_mb - demand)
    elif requirement.vram_mb:
        capacity(FitCode.VRAM_OK, FitCode.VRAM_INSUFFICIENT, requirement.vram_mb, hardware.vram_mb, "_mb")
    elif requirement.unified_memory_mb:
        reasons.append(_reason(FitCode.VRAM_REQUIREMENT_UNDECLARED, unified_required_mb=requirement.unified_memory_mb))
    else:
        reasons.append(_reason(FitCode.VRAM_NOT_REQUIRED))
    if requirement.gpu_required:
        reasons.append(_reason(FitCode.GPU_PRESENT if hardware.gpu is not None else FitCode.GPU_MISSING))
    else:
        reasons.append(_reason(FitCode.GPU_NOT_REQUIRED))
    reasons.append(_accelerators(requirement.accelerators, hardware))
    fits = not any(item.split(":", 1)[0] in FAILING for item in reasons)
    return Assessment(hardware.resource_id, fits, tuple(reasons), min(headroom) if headroom else None)


def _accelerators(required: Sequence[str], hardware: HardwareProfile) -> str:
    if not required:
        return _reason(FitCode.ACCELERATOR_NOT_REQUIRED)
    known = {item.value for item in AcceleratorRuntime}
    unknown = sorted(item for item in required if item not in known)
    if unknown:
        return _reason(FitCode.ACCELERATOR_UNKNOWN, names="|".join(unknown))
    available = sorted((AcceleratorRuntime(item) for item in set(required) if AcceleratorRuntime(item)
                        in hardware.runtimes), key=RUNTIME_ORDER.__getitem__)
    if available:
        return _reason(FitCode.ACCELERATOR_AVAILABLE, runtime=available[0].value)
    return _reason(FitCode.ACCELERATOR_UNAVAILABLE, required="|".join(sorted(set(required))))


def requirement_for(model: ModelRuntimeProfile, runtime: AcceleratorRuntime) -> ResourceRequirement:
    """The resource requirement of running ``model`` on one runtime path (declared figures only)."""
    if runtime not in model.runtimes:
        raise ValueError("MODEL_RUNTIME_NOT_DECLARED")
    if runtime in GPU_RUNTIMES:
        return ResourceRequirement(cpu_cores=model.min_cpu_cores, gpu_required=True,
                                   vram_mb=0 if runtime is AcceleratorRuntime.METAL else model.vram_mb,
                                   unified_memory_mb=model.unified_memory_mb, accelerators=(runtime.value,))
    return ResourceRequirement(cpu_cores=model.min_cpu_cores, ram_mb=model.ram_mb, accelerators=(runtime.value,))


@dataclass(frozen=True)
class RuntimeOption:
    runtime: AcceleratorRuntime
    fits: bool
    reasons: tuple[str, ...]
    headroom_mb: int | None


@dataclass(frozen=True)
class Feasibility:
    """Whether a model fits a machine, per declared runtime path (runtime declaration order)."""

    model_id: str
    resource_id: str
    options: tuple[RuntimeOption, ...]

    @property
    def fits(self) -> bool:
        return any(option.fits for option in self.options)

    @property
    def fitting_runtimes(self) -> tuple[AcceleratorRuntime, ...]:
        return tuple(option.runtime for option in self.options if option.fits)

    @property
    def headroom_mb(self) -> int | None:
        """Largest headroom among fitting options (information only; not a ranking)."""
        values = [option.headroom_mb for option in self.options if option.fits and option.headroom_mb is not None]
        return max(values) if values else None


class StaticResourceInventory:
    """Declared hardware evaluated at one fixed instant. Registration here is never authorization."""

    def __init__(self, hardware: Iterable[HardwareProfile], *, as_of: datetime,
                 max_telemetry_age: timedelta = DEFAULT_MAX_TELEMETRY_AGE,
                 max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW) -> None:
        indexed: dict[str, HardwareProfile] = {}
        for profile in hardware:
            if not isinstance(profile, HardwareProfile):
                raise TypeError("HARDWARE_PROFILE_TYPE_INVALID")
            if profile.resource_id in indexed:
                raise ValueError("INVENTORY_RESOURCE_DUPLICATE")
            indexed[profile.resource_id] = profile
        if len(indexed) > MAX_RESOURCES:
            raise ValueError("INVENTORY_TOO_LARGE")
        if max_telemetry_age <= timedelta(0) or max_future_skew < timedelta(0):
            raise ValueError("INVENTORY_TELEMETRY_BOUNDS_INVALID")
        self._hardware = dict(sorted(indexed.items()))
        self._as_of = aware(as_of, "INVENTORY_AS_OF_NOT_AWARE")
        self._max_age = max_telemetry_age
        self._skew = max_future_skew

    @property
    def resource_ids(self) -> tuple[str, ...]:
        return tuple(self._hardware)

    def hardware(self, resource_id: str) -> HardwareProfile:
        try:
            return self._hardware[resource_id]
        except KeyError:
            raise KeyError(FitCode.RESOURCE_NOT_IN_INVENTORY.value) from None

    def _telemetry(self, hardware: HardwareProfile) -> str:
        return freshness(hardware, as_of=self._as_of, max_age=self._max_age, max_future_skew=self._skew)

    def assess(self, requirement: ResourceRequirement) -> tuple[Assessment, ...]:
        return tuple(assess(requirement, item, telemetry=self._telemetry(item)) for item in self._hardware.values())

    def match(self, requirement: ResourceRequirement) -> tuple[ResourceMatch, ...]:
        """``ResourceInventory`` seam: one explained match per resource, sorted by ``resource_id``."""
        return tuple(item.as_match() for item in self.assess(requirement))

    def feasibility(self, model: ModelRuntimeProfile, resource_id: str) -> Feasibility:
        hardware = self.hardware(resource_id)
        telemetry = self._telemetry(hardware)
        options = []
        for runtime in sorted(model.runtimes, key=RUNTIME_ORDER.__getitem__):
            result = assess(requirement_for(model, runtime), hardware, telemetry=telemetry)
            options.append(RuntimeOption(runtime, result.fits, result.reasons, result.headroom_mb))
        return Feasibility(model.model_id, resource_id, tuple(options))


def resource_fit(matches: Sequence[ResourceMatch], resource_id: str) -> bool:
    """The ``resource_fit`` input for ``pados.capability.policy.evaluate``: unknown resources never fit."""
    found = [item for item in matches if item.resource_id == resource_id]
    return len(found) == 1 and found[0].fits
