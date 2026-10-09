# SPDX-License-Identifier: AGPL-3.0-only
"""The adapter protocol and the trusted stage boundary.

Trust model (explicit): adapters are operator-installed code running in the PADOS process. The contract
enforces POLICY isolation, not a sandbox: an adapter is only ever invoked through ``run_bound_stage`` with a
``StageBinding`` (validated descriptors, an ALLOWED policy decision bound to the validated grant scope,
project, stage, classification and network policy) and a ``ScopedArtifacts`` facade that can read only the
stage's authorized inputs. It receives no database session, proof service, CAS/ScopeVersion handle or
Memory writer. Python cannot stop in-process code from importing modules or opening sockets; adapters that
are not trusted operator code must run through the existing Sandbox Fabric, and network use must be
declared (``network_requirement``) so policy refuses it where the task forbids it. Outputs are candidates
(``semantic_authority = NONE``).
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from pados.capability.artifacts import ArtifactRef, ArtifactStore, ScopedArtifacts
from pados.capability.model import AdapterDescriptor, CapabilityDescriptor, CapabilityFamily, descriptor_digest
from pados.capability.policy import AuthorizedScope, PolicyContext, PolicyDecision
from pados.enterprise.classification import DataClassification
from pados.lifecycle.minimization import assert_no_credentials

MAX_METRICS = 32
MAX_EVIDENCE_KEYS = 32


@dataclass(frozen=True)
class StageRequest:
    stage_id: str
    capability_id: str
    inputs: tuple[ArtifactRef, ...]
    parameters: Mapping[str, Any]
    classification: DataClassification
    seed: int | None = None
    router_selection_hash: str | None = None  # Worker Router evidence (required for MODEL_RUNTIME stages)


@dataclass(frozen=True)
class StageContext:
    """What an adapter may touch: a stage-scoped artifact facade, nothing else."""

    artifacts: ScopedArtifacts
    project_id: str
    stage_id: str


def _frozen_metrics(metrics: Mapping[str, float]) -> Mapping[str, float]:
    if len(metrics) > MAX_METRICS:
        raise ValueError("STAGE_METRICS_TOO_MANY")
    cleaned: dict[str, float] = {}
    for key, value in metrics.items():
        number = float(value)
        if not isinstance(key, str) or not key or len(key) > 64 or not math.isfinite(number):
            raise ValueError("STAGE_METRIC_INVALID")
        cleaned[key] = number
    return MappingProxyType(cleaned)


@dataclass(frozen=True)
class StageOutput:
    outputs: tuple[ArtifactRef, ...]
    metrics: Mapping[str, float] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)  # bounded, content-free (hashes, counts, scores)
    partial: bool = False
    semantic_authority: str = "NONE"

    def __post_init__(self) -> None:
        if self.semantic_authority != "NONE":
            raise ValueError("CAPABILITY_OUTPUT_CANNOT_CARRY_AUTHORITY")
        if len(self.evidence) > MAX_EVIDENCE_KEYS:
            raise ValueError("STAGE_EVIDENCE_TOO_LARGE")
        for key, value in self.evidence.items():  # flat and immutable: scalars or short strings only
            if not isinstance(key, str) or not (value is None or isinstance(value, (bool, int, float, str))):
                raise ValueError("STAGE_EVIDENCE_NOT_FLAT")
            if isinstance(value, str) and len(value) > 256:
                raise ValueError("STAGE_EVIDENCE_VALUE_TOO_LONG")
        assert_no_credentials(dict(self.evidence))
        object.__setattr__(self, "metrics", _frozen_metrics(self.metrics))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


class CapabilityAdapterError(RuntimeError):
    """Adapter failure with a closed code; never echoes input content."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class CapabilityAdapter(Protocol):
    @property
    def descriptor(self) -> AdapterDescriptor: ...

    def available(self) -> bool:
        """Side-effect free readiness probe; called only after every policy check has passed."""

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput: ...


@dataclass(frozen=True)
class StageBinding:
    """A request bound to everything that allowed it. Built only by ``bind_stage``."""

    capability: CapabilityDescriptor
    adapter: AdapterDescriptor
    context: PolicyContext
    decision: PolicyDecision
    request: StageRequest
    capability_digest: str
    adapter_digest: str


class StageBindingError(PermissionError):
    pass


def bind_stage(capability: CapabilityDescriptor, adapter: AdapterDescriptor, context: PolicyContext,
               decision: PolicyDecision, request: StageRequest, *,
               permitted_inputs: frozenset[str]) -> StageBinding:
    """Fail closed unless request, descriptors and the ALLOWED decision all describe the same stage."""
    expected = dict(decision.binding)
    if not decision.allowed:
        raise StageBindingError("POLICY_DECISION_NOT_ALLOWED")
    if (decision.capability_id != capability.capability_id or decision.adapter_id != adapter.adapter_id
            or expected.get("capability_digest") != descriptor_digest(capability)
            or expected.get("adapter_digest") != descriptor_digest(adapter)):
        raise StageBindingError("POLICY_DECISION_DESCRIPTOR_MISMATCH")
    if (expected.get("project_id") != context.project_id or expected.get("stage_id") != request.stage_id
            or expected.get("grant_hash") != context.scope.grant_hash
            or expected.get("classification") != request.classification.value
            or request.classification is not context.classification):
        raise StageBindingError("POLICY_DECISION_CONTEXT_MISMATCH")
    if request.capability_id != capability.capability_id or context.stage_id != request.stage_id:
        raise StageBindingError("STAGE_REQUEST_MISMATCH")
    if any(ref.sha256 not in permitted_inputs for ref in request.inputs):
        raise StageBindingError("STAGE_INPUT_NOT_PERMITTED")
    return StageBinding(capability, adapter, context, decision, request,
                        descriptor_digest(capability), descriptor_digest(adapter))


def run_bound_stage(binding: StageBinding, adapter: CapabilityAdapter, store: ArtifactStore, *,
                    revalidate: Callable[[], AuthorizedScope],
                    max_outputs: int = 32, max_output_bytes: int = 64 * 1024 * 1024) -> StageOutput:
    """The only sanctioned way to call ``adapter.execute``.

    ``revalidate`` must re-run ``authorize_scope`` with the CURRENT grant document; a revoked or changed
    grant, a changed adapter descriptor, or a model-inference stage without Worker Router evidence refuses.
    """
    if descriptor_digest(adapter.descriptor) != binding.adapter_digest:
        raise StageBindingError("ADAPTER_DESCRIPTOR_CHANGED_SINCE_BINDING")
    try:
        current = revalidate()
    except PermissionError as exc:
        raise StageBindingError("AUTHORIZATION_REVOKED_BEFORE_EXECUTION") from exc
    if (current.grant_hash != binding.context.scope.grant_hash or current.project_id != binding.context.project_id
            or binding.adapter.grant_capability not in current.capabilities):
        raise StageBindingError("AUTHORIZATION_CHANGED_BEFORE_EXECUTION")
    if binding.capability.family is CapabilityFamily.MODEL_RUNTIME and not (
            isinstance(binding.request.router_selection_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", binding.request.router_selection_hash)):
        # Syntax only here; the executor must resolve the hash to the Worker Router bridge's own evidence.
        raise StageBindingError("MODEL_INFERENCE_REQUIRES_WORKER_ROUTER_SELECTION")
    scoped = ScopedArtifacts(store, readable=binding.request.inputs, max_outputs=max_outputs,
                             max_output_bytes=max_output_bytes)
    context = StageContext(artifacts=scoped, project_id=binding.context.project_id,
                           stage_id=binding.request.stage_id)
    output = adapter.execute(binding.request, context=context)
    if not isinstance(output, StageOutput):
        raise StageBindingError("ADAPTER_OUTPUT_INVALID")
    if any(ref not in scoped.written for ref in output.outputs):
        raise StageBindingError("ADAPTER_OUTPUT_NOT_WRITTEN_THROUGH_STAGE")
    return output
