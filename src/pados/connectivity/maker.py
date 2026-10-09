# SPDX-License-Identifier: AGPL-3.0-only
"""Connectivity-aware Direct Maker wrappers.

Both wrap an existing Maker. Before dispatch the connectivity gate is
consulted; an unreachable cloud provider produces the existing failed
RuntimeResult shape (RUNTIME_FAILURE, retryable) with summary
PROVIDER_UNREACHABLE, so the product loop's normal validation refuses to commit.
Invalid connectivity metadata is a BLOCKED result (AUTHORITY_BLOCK, not
retryable). Neither wrapper ever reroutes to another (e.g. local) provider.

* ``ConnectivityGatedMaker``: endpoint-based (``WanEgressGate``).
* ``RequirementGatedMaker``: ``NetworkRequirement``-based; only WAN_REQUIRED
  is gated on a fresh WAN observation. LOCAL_ONLY / PRIVATE_NETWORK /
  NETWORK_OPTIONAL dispatch without WAN.
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from pados.connectivity.egress import PROVIDER_UNREACHABLE, ProviderUnreachable, WanEgressGate
from pados.connectivity.requirements import ConnectivityGateBlocked, NetworkRequirement
from pados.connectivity.state import ConnectivityState
from pados.product_loop.maker import DirectMaker
from pados.product_loop.schemas import FailureClass, RuntimeResult, RuntimeStatus, WorkContract

_WAN_USABLE = frozenset({ConnectivityState.CONNECTED, ConnectivityState.RECOVERING})


def _gate_result(run_id: str, attempt: int, summary: str, *, blocked: bool = False) -> RuntimeResult:
    stamp = datetime.now(UTC).isoformat()
    return RuntimeResult(
        run_id=run_id, attempt=attempt, runtime_id=f"connectivity-gate-{uuid4()}",
        status=RuntimeStatus.BLOCKED if blocked else RuntimeStatus.FAILED, started_at=stamp, completed_at=stamp,
        summary=summary,
        failure_classification=FailureClass.AUTHORITY_BLOCK if blocked else FailureClass.RUNTIME_FAILURE,
        retryable=not blocked, completion_declared=True,
    )


class ConnectivityGatedMaker:
    def __init__(self, inner: DirectMaker, *, endpoint: str, gate: WanEgressGate) -> None:
        self.inner = inner
        self.endpoint = endpoint
        self.gate = gate
        self.approved_root = getattr(inner, "approved_root", None)

    async def make(self, contract: WorkContract, *, run_id: str, attempt: int) -> RuntimeResult:
        try:
            self.gate.check(self.endpoint)
        except ProviderUnreachable:
            return _gate_result(run_id, attempt, PROVIDER_UNREACHABLE)
        return await self.inner.make(contract, run_id=run_id, attempt=attempt)


@dataclass(frozen=True)
class WanObservation:
    observable: bool  # False: connectivity configured but no WAN probe -> cannot be established
    state: ConnectivityState
    age_seconds: float | None  # None: never observed


class WanStateSource(Protocol):
    max_age_seconds: float

    def wan_observation(self) -> WanObservation:
        """Current WAN observation with its age."""

    def refresh(self) -> None:
        """One bounded synchronous observation (per-probe timeout <= 10 s)."""


class GateReason(StrEnum):
    """Internal gate diagnostics; the RuntimeResult summary stays PROVIDER_UNREACHABLE."""

    WAN_FRESH_USABLE = "WAN_FRESH_USABLE"
    WAN_NOT_OBSERVABLE = "WAN_NOT_OBSERVABLE"
    WAN_OBSERVATION_STALE = "WAN_OBSERVATION_STALE"
    WAN_UNAVAILABLE = "WAN_UNAVAILABLE"
    NOT_WAN_REQUIRED = "NOT_WAN_REQUIRED"
    BLOCKED = "BLOCKED"


class RequirementGatedMaker:
    """WAN_REQUIRED dispatch needs a FRESH, usable WAN observation (age <=
    ``wan.max_age_seconds``). A stale or missing observation gets exactly one
    bounded synchronous refresh; if it is still not fresh, or WAN is not usable,
    or WAN is not observable at all, dispatch fails closed."""

    def __init__(self, inner: DirectMaker, *, requirement: Callable[[], NetworkRequirement],
                 wan: WanStateSource) -> None:
        self.inner = inner
        self.requirement = requirement
        self.wan = wan
        self.approved_root = getattr(inner, "approved_root", None)
        self.diagnostics: deque[dict[str, str]] = deque(maxlen=32)  # R0, in memory only

    def _fresh(self, observation: WanObservation) -> bool:
        return (observation.state is not ConnectivityState.UNKNOWN and observation.age_seconds is not None
                and observation.age_seconds <= self.wan.max_age_seconds)

    async def _wan_reason(self) -> GateReason:
        observation = self.wan.wan_observation()
        if not observation.observable:
            return GateReason.WAN_NOT_OBSERVABLE
        if not self._fresh(observation):
            try:
                await asyncio.to_thread(self.wan.refresh)
            except OSError:
                return GateReason.WAN_OBSERVATION_STALE
            observation = self.wan.wan_observation()
            if not self._fresh(observation):
                return GateReason.WAN_OBSERVATION_STALE
        return GateReason.WAN_FRESH_USABLE if observation.state in _WAN_USABLE else GateReason.WAN_UNAVAILABLE

    async def make(self, contract: WorkContract, *, run_id: str, attempt: int) -> RuntimeResult:
        try:
            requirement = self.requirement()
        except ConnectivityGateBlocked as exc:
            self.diagnostics.append({"reason": GateReason.BLOCKED.value, "code": exc.code})
            return _gate_result(run_id, attempt, exc.code, blocked=True)
        if requirement is not NetworkRequirement.WAN_REQUIRED:
            self.diagnostics.append({"reason": GateReason.NOT_WAN_REQUIRED.value, "requirement": requirement.value})
            return await self.inner.make(contract, run_id=run_id, attempt=attempt)
        reason = await self._wan_reason()
        self.diagnostics.append({"reason": reason.value, "requirement": requirement.value})
        if reason is not GateReason.WAN_FRESH_USABLE:
            return _gate_result(run_id, attempt, PROVIDER_UNREACHABLE)
        return await self.inner.make(contract, run_id=run_id, attempt=attempt)
