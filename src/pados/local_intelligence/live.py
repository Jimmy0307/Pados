# SPDX-License-Identifier: AGPL-3.0-only
"""Live device and runtime state for REGISTERED measured profiles (observation only; never authority).

This is the adapter between the measuring stack and the generic binding contract
(``pados.resources.capability_profile.bind_live``): the existing ``DeviceProfiler`` and runtime discovery
(``describe_runtime``) are run on demand, and their descriptors are projected into the generic
``DeviceObservation`` / ``RuntimeObservation``. Runtime-kind knowledge stays in the discovery probes; nothing here
names a runtime, vendor or model.

* Device identity uses the SAME stable-fact derivation as the measured profile (``device_profile_id``), so a
  different machine, GPU, driver or OS version is a different device and the evidence does not apply to it.
* Observations carry their time. ``LiveLocalState`` re-observes after ``refresh_seconds``; when re-observation
  fails the previous observation keeps its original time and ages into STALE under the binding policy, so stale
  state is never silently trusted.
* Nothing here authorizes, downloads, pulls, promotes lifecycle or marks anything routable.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pados.local_intelligence.benchmark import normalize_digest
from pados.local_intelligence.device import DeviceProfile, DeviceProfiler, GpuStatus
from pados.local_intelligence.profile_builder import device_profile_id
from pados.local_intelligence.runtimes import RuntimeDescriptor, RuntimeEndpoint, RuntimeHealth, describe_runtime
from pados.resources.capability_profile import (
    BindingPolicy,
    CandidateState,
    DeviceObservation,
    LiveRuntimeBinding,
    ModelCapabilityProfile,
    RuntimeArtifact,
    RuntimeObservation,
    bind_live,
)

LIVE_STATE_SCHEMA = "pados.local-intelligence.live-state.v1"


def device_observation(profile: DeviceProfile) -> DeviceObservation:
    """Measured facts only: identity from stable facts, headroom from the same profiling pass."""
    reasons: list[str] = []
    state = CandidateState.AVAILABLE
    if profile.memory is None:
        reasons.append("DEVICE_MEMORY_UNKNOWN")
        state = CandidateState.UNAVAILABLE
    free_gpu: int | None = 0
    if profile.gpu.status is GpuStatus.GPU_PRESENT and profile.gpu.devices:
        device = max(profile.gpu.devices, key=lambda item: (item.vram_total_mb, -item.index))
        free_gpu = device.vram_free_mb  # None when the profiler could not read it: telemetry UNKNOWN
    elif profile.gpu.status is GpuStatus.GPU_DISCOVERY_FAILED:
        reasons.append("GPU_DISCOVERY_FAILED")
        state = CandidateState.DEGRADED if state is CandidateState.AVAILABLE else state
        free_gpu = None
    return DeviceObservation(
        device_profile_id=device_profile_id(profile.evidence()), state=state, reasons=tuple(reasons),
        free_gpu_memory_mib=free_gpu, available_ram_mib=profile.memory.available_mb if profile.memory else None,
        observed_at=profile.measured_at)


_HEALTH_STATE = {RuntimeHealth.HEALTHY: CandidateState.AVAILABLE, RuntimeHealth.PROBE_FAILED: CandidateState.DEGRADED,
                 RuntimeHealth.UNREACHABLE: CandidateState.UNAVAILABLE, RuntimeHealth.NOT_PROBED: CandidateState.UNAVAILABLE}


def runtime_observation(descriptor: RuntimeDescriptor) -> RuntimeObservation:
    state = _HEALTH_STATE[descriptor.health]
    reasons = () if state is CandidateState.AVAILABLE else (f"RUNTIME_{descriptor.health.value}",)
    return RuntimeObservation(
        runtime_id=descriptor.runtime_id, runtime_family=descriptor.runtime_type, runtime_version=descriptor.version,
        state=state, reasons=reasons,
        artifacts=tuple(RuntimeArtifact(runtime_ref=m.name, manifest_digest=normalize_digest(m.digest))
                        for m in descriptor.models),
        resident_refs=tuple(m.name for m in descriptor.loaded_models), observed_at=descriptor.last_probe)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class LiveLocalState:
    """Current device and runtime observations, refreshed on demand (thread-safe; bounded refresh rate)."""

    profiler: Callable[[], DeviceProfile]
    endpoints: tuple[RuntimeEndpoint, ...]
    clock: Callable[[], datetime] = _now
    refresh_seconds: float = 15.0
    describe: Callable[[RuntimeEndpoint, datetime], RuntimeDescriptor] = field(
        default=lambda target, now: describe_runtime(target, now=now))
    failures: list[str] = field(default_factory=list)
    _device: DeviceObservation | None = None
    _device_raw: DeviceProfile | None = None
    _runtimes: dict[str, RuntimeObservation] = field(default_factory=dict)
    _runtime_raw: dict[str, RuntimeDescriptor] = field(default_factory=dict)
    _refreshed: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def local(cls, endpoints: tuple[RuntimeEndpoint, ...], **kwargs: object) -> LiveLocalState:
        return cls(profiler=lambda: DeviceProfiler.local().profile(), endpoints=endpoints, **kwargs)  # type: ignore[arg-type]

    def refresh(self, *, force: bool = False) -> None:
        with self._lock:
            now = self.clock()
            if not force and self._refreshed is not None \
                    and 0 <= (now - self._refreshed).total_seconds() < self.refresh_seconds:
                return  # (a clock that moved backwards always re-observes)
            self._refreshed = now
            try:
                raw = self.profiler()
                self._device, self._device_raw = device_observation(raw), raw
            except Exception as exc:  # noqa: BLE001 - keep the old observation; it ages into STALE
                self.failures.append(f"DEVICE_PROFILER_FAILED:{type(exc).__name__}")
            for target in self.endpoints:
                try:
                    descriptor = self.describe(target, now)
                    self._runtimes[target.runtime_id] = runtime_observation(descriptor)
                    self._runtime_raw[target.runtime_id] = descriptor
                except Exception as exc:  # noqa: BLE001 - discovery never raises by contract; fail closed if it does
                    self.failures.append(f"RUNTIME_DISCOVERY_FAILED:{type(exc).__name__}")

    def observations(self, *, force: bool = False) -> tuple[DeviceObservation | None, tuple[RuntimeObservation, ...]]:
        self.refresh(force=force)
        with self._lock:
            return self._device, tuple(self._runtimes[key] for key in sorted(self._runtimes))

    def bindings(self, profiles: Iterable[ModelCapabilityProfile], policy: BindingPolicy, *,
                 force: bool = False) -> tuple[tuple[ModelCapabilityProfile, LiveRuntimeBinding], ...]:
        device, runtimes = self.observations(force=force)
        now = self.clock()
        return tuple((profile, bind_live(profile, runtimes, device, now=now, policy=policy)) for profile in profiles)

    def evidence(self) -> dict[str, object]:
        """The raw descriptors last observed (for reports; endpoints are already scheme://host:port only)."""
        with self._lock:
            return {"schema": LIVE_STATE_SCHEMA, "refreshed_at": self._refreshed.isoformat() if self._refreshed else None,
                    "device": self._device.model_dump(mode="json") if self._device else None,
                    "device_profile": self._device_raw.evidence() if self._device_raw else None,
                    "runtimes": {k: v.model_dump(mode="json") for k, v in sorted(self._runtimes.items())},
                    "runtime_descriptors": {k: v.model_dump(mode="json") for k, v in sorted(self._runtime_raw.items())},
                    "failures": list(self.failures[-20:])}
