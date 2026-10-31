# SPDX-License-Identifier: AGPL-3.0-only
"""REFERENCE_DIGEST: a deterministic, content-free digest of one input artifact, packaged as a Capability Pack V1.

It exists to exercise the pack contract end to end (manifest validation, registration through the existing
registry, execution only through the existing executor, required evidence, closed failure codes, SCHEMA
verification and deterministic replay). The output names the input only by its SHA-256 plus byte and line counts
-- never its content. Output is a candidate (``semantic_authority = NONE``).
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactRef
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    FailureSemantics,
    FallbackPolicy,
    LicenseInfo,
    LifecycleState,
    Modality,
    ResourceRequirement,
    VerificationMethod,
)
from pados.capability.pack import (
    CORE_EVIDENCE,
    CapabilityPackManifest,
    CheckerSpec,
    Egress,
    FailureCodeSpec,
    PackProvenance,
    PackProvider,
    PackRuntime,
    PackSecurity,
    PackSource,
    RuntimeType,
)
from pados.capability_loop.contracts import CapabilityState
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash

PACK_ID = "pados.reference.digest"
CAPABILITY_ID = "REFERENCE_DIGEST"
ADAPTER_ID = "reference.digest"
INPUT_CONTRACT = "reference.blob/v1"
OUTPUT_CONTRACT = "reference.digest/v1"
CHECKER_ID = "reference.digest.schema"
OUTPUT_KIND = "reference.digest"
OUTPUT_SCHEMA = "pados.reference.digest.v1"
JSON_MEDIA_TYPE = "application/json"
RUNTIME_VERSION = "1.0.0"
DEVICE = "cpu"
CONFIG_DIGEST = canonical_hash({"algorithm": "sha256+line-count", "schema": OUTPUT_SCHEMA})
_OUTPUT_KEYS = frozenset({"schema", "input_sha256", "bytes", "lines"})
MAX_OUTPUT_BYTES = 512

CAPABILITY = CapabilityDescriptor(
    capability_id=CAPABILITY_ID, version="1", family=CapabilityFamily.ALGORITHM, operation="DIGEST_INPUT",
    input_contract=INPUT_CONTRACT, output_contract=OUTPUT_CONTRACT, determinism=Determinism.DETERMINISTIC,
    modalities=(Modality.TEXT,), max_classification=DataClassification.INTERNAL,
    network_requirement=NetworkRequirement.LOCAL_ONLY,
    verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
    failure=FailureSemantics.FAIL_CLOSED, fallback=FallbackPolicy.NONE, lifecycle=LifecycleState.EXPERIMENTAL,
    description="Content-free digest of one input (reference pack)")

ADAPTER = AdapterDescriptor(
    adapter_id=ADAPTER_ID, capability_id=CAPABILITY_ID, version="1", backend="python",
    determinism=Determinism.DETERMINISTIC, max_classification=DataClassification.INTERNAL,
    network_requirement=NetworkRequirement.LOCAL_ONLY, license=LicenseInfo(spdx="AGPL-3.0-only"),
    lifecycle=LifecycleState.EXPERIMENTAL)

MANIFEST = CapabilityPackManifest(
    pack_id=PACK_ID, version="1",
    provider=PackProvider(provider_id="pados.reference", runtime_id="python", runtime_version=RUNTIME_VERSION),
    provenance=PackProvenance(source=PackSource.IN_TREE, license=LicenseInfo(spdx="AGPL-3.0-only"),
                              publisher="pados"),
    capability_ids=(CAPABILITY_ID,), capabilities=(CAPABILITY,), adapters=(ADAPTER,),
    runtime=PackRuntime(runtime_type=RuntimeType.IN_PROCESS, device=(ResourceRequirement(cpu_cores=1),),
                        execution_config_digest=CONFIG_DIGEST, timeout_seconds=30,
                        network=NetworkRequirement.LOCAL_ONLY),
    security=PackSecurity(classification_ceiling=DataClassification.INTERNAL, egress=Egress.NONE),
    evidence_requirements=(*CORE_EVIDENCE, "output_contract"),
    verification_hooks=(CheckerSpec(output_contract=OUTPUT_CONTRACT, checker_id=CHECKER_ID),),
    initial_state=CapabilityState.EXPERIMENTAL,
    failure_codes=(FailureCodeSpec(code="REFERENCE_INPUT_COUNT_INVALID"),
                   FailureCodeSpec(code="REFERENCE_INPUT_EMPTY")))


def _canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class ReferenceDigestAdapter:
    """Deterministic implementation; registered only wrapped in ``GovernedPackAdapter`` (``register_pack``)."""

    def __init__(self) -> None:
        self._descriptor = ADAPTER

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        if len(request.inputs) != 1:
            raise CapabilityAdapterError("REFERENCE_INPUT_COUNT_INVALID")
        source = request.inputs[0]
        data = context.artifacts.get(source)
        if not data:
            raise CapabilityAdapterError("REFERENCE_INPUT_EMPTY")
        record = {"schema": OUTPUT_SCHEMA, "input_sha256": source.sha256, "bytes": len(data),
                  "lines": data.count(b"\n") + (0 if data.endswith(b"\n") else 1)}
        ref = context.artifacts.put(_canonical(record), media_type=JSON_MEDIA_TYPE, kind=OUTPUT_KIND)
        evidence = {"runtime_version": RUNTIME_VERSION, "device": DEVICE, "config_digest": CONFIG_DIGEST,
                    "provider_id": MANIFEST.provider.provider_id, "artifact_digest": ref.sha256,
                    "output_contract": OUTPUT_CONTRACT}
        return StageOutput(outputs=(ref,), metrics={"bytes": float(len(data))}, evidence=evidence)


def adapter() -> ReferenceDigestAdapter:
    return ReferenceDigestAdapter()


def digest_output_valid(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
    """SCHEMA check for ``reference.digest/v1``: exactly one canonical, content-free digest record."""
    if len(outputs) != 1:
        return False
    ref, payload = outputs[0]
    if ref.kind != OUTPUT_KIND or len(payload) > MAX_OUTPUT_BYTES:
        return False
    try:
        record = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return False
    return (isinstance(record, dict) and set(record) == _OUTPUT_KEYS and record["schema"] == OUTPUT_SCHEMA
            and isinstance(record["input_sha256"], str) and len(record["input_sha256"]) == 64
            and all(isinstance(record[k], int) and not isinstance(record[k], bool) and record[k] >= 0
                    for k in ("bytes", "lines"))
            and _canonical(record) == payload)
