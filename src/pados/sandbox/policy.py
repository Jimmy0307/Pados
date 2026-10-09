# SPDX-License-Identifier: AGPL-3.0-only
"""Sandbox profiles, network policy and data-egress contracts (default deny)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pados.enterprise.classification import DataClassification


class SandboxProfile(StrEnum):
    L3_ISOLATED = "L3_ISOLATED"  # default: routed isolation, deny-by-default
    L3_REDTEAM = "L3_REDTEAM"  # isolated lab segment, synthetic targets only
    OFFLINE_SANDBOX = "OFFLINE_SANDBOX"  # no network at all
    L2_SECURITY_LAB = "L2_SECURITY_LAB"  # explicit opt-in only, never default


DEFAULT_PROFILE = SandboxProfile.L3_ISOLATED


class IsolationClass(StrEnum):
    HOST_NATIVE = "HOST_NATIVE"
    CONTAINER = "CONTAINER"
    VM_SANDBOX = "VM_SANDBOX"
    REMOTE_NODE = "REMOTE_NODE"


class Zone(StrEnum):
    SANDBOX = "SANDBOX"
    LAB_SEGMENT = "LAB_SEGMENT"
    PADOS_ARTIFACT_EXCHANGE = "PADOS_ARTIFACT_EXCHANGE"
    HOST = "HOST"
    LAN = "LAN"
    INTERNET = "INTERNET"
    MANAGEMENT = "MANAGEMENT"


class Direction(StrEnum):
    INGRESS = "INGRESS"
    EGRESS = "EGRESS"


class Action(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True)
class NetworkRule:
    source: Zone
    destination: Zone
    direction: Direction
    protocol: str  # "tcp" | "udp" | "any"
    port: int | None  # None = any
    action: Action


@dataclass(frozen=True)
class NetworkPolicy:
    profile: SandboxProfile
    rules: tuple[NetworkRule, ...] = ()
    internet_egress: bool = False
    management_access: bool = False
    default_action: Action = Action.DENY

    def decide(
        self, *, source: Zone, destination: Zone, direction: Direction, protocol: str, port: int
    ) -> Action:
        if destination is Zone.INTERNET and not self.internet_egress:
            return Action.DENY
        if Zone.MANAGEMENT in {source, destination} and not self.management_access:
            return Action.DENY
        # Sandboxes can never reach the host or the LAN, whatever a rule says.
        if source in {Zone.SANDBOX, Zone.LAB_SEGMENT} and destination in {Zone.HOST, Zone.LAN}:
            return Action.DENY
        for rule in self.rules:
            if (
                rule.source is source
                and rule.destination is destination
                and rule.direction is direction
                and rule.protocol in {protocol, "any"}
                and rule.port in {port, None}
            ):
                return rule.action
        return self.default_action


class PolicyError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def profile_policy(profile: SandboxProfile) -> NetworkPolicy:
    """Built-in network policy per profile. None of them allow Internet egress."""
    exchange = NetworkRule(
        Zone.SANDBOX, Zone.PADOS_ARTIFACT_EXCHANGE, Direction.EGRESS, "tcp", 443, Action.ALLOW
    )
    if profile is SandboxProfile.OFFLINE_SANDBOX:
        return NetworkPolicy(profile=profile)
    if profile is SandboxProfile.L3_REDTEAM:
        lab = NetworkRule(Zone.SANDBOX, Zone.LAB_SEGMENT, Direction.EGRESS, "any", None, Action.ALLOW)
        return NetworkPolicy(profile=profile, rules=(exchange, lab))
    return NetworkPolicy(profile=profile, rules=(exchange,))


@dataclass(frozen=True)
class SandboxRequest:
    profile: SandboxProfile = DEFAULT_PROFILE
    isolation: IsolationClass = IsolationClass.VM_SANDBOX
    lab_targets: tuple[str, ...] = ()
    explicit_l2_opt_in: bool = False


_SYNTHETIC_TARGET_PREFIX = "lab-synthetic:"


def validate_request(request: SandboxRequest) -> SandboxRequest:
    if request.profile is SandboxProfile.L2_SECURITY_LAB and not request.explicit_l2_opt_in:
        raise PolicyError("L2_PROFILE_REQUIRES_EXPLICIT_OPT_IN")
    if request.profile in {SandboxProfile.L3_REDTEAM, SandboxProfile.L2_SECURITY_LAB}:
        if request.isolation is not IsolationClass.VM_SANDBOX:
            raise PolicyError("LAB_PROFILE_REQUIRES_VM_ISOLATION")
        if not request.lab_targets:
            raise PolicyError("LAB_TARGETS_REQUIRED")
        for target in request.lab_targets:
            if not target.startswith(_SYNTHETIC_TARGET_PREFIX):
                raise PolicyError("LAB_TARGET_MUST_BE_SYNTHETIC")
    elif request.lab_targets:
        raise PolicyError("LAB_TARGETS_NOT_ALLOWED_FOR_PROFILE")
    return request


@dataclass(frozen=True)
class DataEgressPolicy:
    classification_ceiling: DataClassification = DataClassification.INTERNAL
    remote_node_eligible: bool = False
    cloud_eligible: bool = False
    internet_egress: bool = False
    artifact_export: bool = True
    max_artifact_bytes: int = 16 * 1024 * 1024
    allowed_artifact_types: frozenset[str] = field(
        default_factory=lambda: frozenset({".txt", ".json", ".md", ".csv", ".log", ".diff", ".patch"})
    )
    sandbox_profile: SandboxProfile = DEFAULT_PROFILE

    def admit_input(self, classification: DataClassification, isolation: IsolationClass) -> None:
        if not self.classification_ceiling.permits(classification):
            raise PolicyError("CLASSIFICATION_ABOVE_EGRESS_CEILING")
        if isolation is IsolationClass.REMOTE_NODE and not self.remote_node_eligible:
            raise PolicyError("REMOTE_NODE_NOT_ELIGIBLE")

    def admit_artifact(self, name: str, size_bytes: int) -> None:
        if not self.artifact_export:
            raise PolicyError("ARTIFACT_EXPORT_DISABLED")
        if size_bytes > self.max_artifact_bytes:
            raise PolicyError("ARTIFACT_TOO_LARGE")
        suffix = name[name.rfind(".") :].lower() if "." in name else ""
        if suffix not in self.allowed_artifact_types:
            raise PolicyError("ARTIFACT_TYPE_NOT_ALLOWED")
