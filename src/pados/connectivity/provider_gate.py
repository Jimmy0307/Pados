# SPDX-License-Identifier: AGPL-3.0-only
"""Default product-loop wiring for the provider connectivity requirement.

The requirement of the product-loop provider is derived from the worker the
existing Worker Router (``select_product_loop_provider`` -> ``select_worker``)
selects, via ``profile_for_worker`` metadata plus optional operator metadata
(``provider_requirements`` keyed by ``provider_id``). This module never ranks,
selects or substitutes a worker itself. Without ``PADOS_CONNECTIVITY_CONFIG``
the maker is returned unchanged.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pados.connectivity.config import ConnectivityConfig, ProbeFactory, load_config
from pados.connectivity.maker import RequirementGatedMaker, WanObservation
from pados.connectivity.requirements import ConnectivityGateBlocked, NetworkRequirement, resolve_requirement
from pados.connectivity.runtime import ConfiguredWan
from pados.connectivity.state import ConnectivityState
from pados.enterprise.compute import profile_for_worker
from pados.product_loop.maker import DirectMaker
from pados.product_loop.provider_selection import select_product_loop_provider
from pados.worker.registry import discover_workers
from pados.worker.schemas import WorkerSpec

WorkerDiscovery = Callable[[Any], Mapping[str, WorkerSpec]]


def product_loop_requirement(settings: Any, discovery: WorkerDiscovery,
                             config: ConnectivityConfig) -> NetworkRequirement:
    try:
        workers = discovery(settings)
    except OSError:
        workers = {}
    selection = select_product_loop_provider(str(getattr(settings, "product_loop_provider", "AUTO")), workers)
    if not selection.selected or selection.worker is None:
        return NetworkRequirement.WAN_REQUIRED  # unknown provider: conservative
    profile = profile_for_worker(selection.worker)
    return resolve_requirement(profile=profile, explicit=config.operator_requirement(profile.provider_id))


class _InvalidConfig:
    max_age_seconds = 0.0

    def wan_observation(self) -> WanObservation:
        return WanObservation(observable=False, state=ConnectivityState.UNKNOWN, age_seconds=None)

    def refresh(self) -> None:
        return None


def gate_product_loop_maker(maker: DirectMaker, settings: Any, *,
                            probe_factory: ProbeFactory | None = None) -> DirectMaker:
    config_path = str(getattr(settings, "connectivity_config", "") or "")
    if not config_path:
        return maker
    discovery: WorkerDiscovery = getattr(maker, "worker_discovery", discover_workers)
    config = load_config(config_path)
    if not isinstance(config, ConnectivityConfig):
        code = str(config["status"])

        def refuse() -> NetworkRequirement:
            raise ConnectivityGateBlocked(code)

        return RequirementGatedMaker(maker, requirement=refuse, wan=_InvalidConfig())
    loaded = config
    return RequirementGatedMaker(
        maker,
        requirement=lambda: product_loop_requirement(settings, discovery, loaded),
        wan=ConfiguredWan(config_path, loaded, probe_factory=probe_factory),
    )
