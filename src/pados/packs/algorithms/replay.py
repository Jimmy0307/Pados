# SPDX-License-Identifier: AGPL-3.0-only
"""DETERMINISTIC_REPLAY verification for algorithm results (evidence for a StageReceipt, never acceptance).

``verify_replay`` recomputes a recorded run from the dataset artifact bytes and the request parameters with
the CURRENT algorithm implementation. Nothing in the record or the result is trusted on its own: the
trusted caller (the executor, from its ``StageBinding`` and the ``StageOutput`` it observed) supplies an
``ExpectedStage`` - capability id and digest, adapter id, version and digest, request parameters hash,
the trusted request SEED and the input and output artifact references - and replay checks every one of
them. The algorithms are DETERMINISTIC, so the bound seed must be ``None`` (execution refuses every seed);
a seed that was not established by the caller (``SEED_UNKNOWN``, the default when ``ExpectedStage`` is
built by hand) fails closed. It also checks the
record's own capability, adapter, request parameters hash (recomputed with the shared
``pados.capability.receipts.parameters_hash`` helper the adapter uses), seed (algorithms are
DETERMINISTIC: a seed is refused exactly as execution refuses it), metrics and result hash against the
recomputation. Every mismatch is FAILED with its own closed code; any unexpected exception is FAILED with
``REPLAY_UNEXPECTED_ERROR`` and no exception escapes. The returned evidence hash and verifier id are what a
``StageReceipt`` needs for ``verification = PASSED``; acceptance still happens only through the existing
receipt -> validation -> CAS path.

Not verifiable by recomputation and therefore NOT covered by a match: the record's ``compute_target``,
``deadline_seconds`` and ``completion`` (operational telemetry only). Wall-clock runtime is never in the
record (an output artifact compared byte for byte by the fabric's replay); it is a stage metric only.

``deadline_seconds`` (optional, at most 3600 s) bounds the recomputation by wall-clock time; it can only
turn a replay into ``REPLAY_ALGORITHM_DEADLINE_EXCEEDED``, never into a different result.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from pados.capability.adapter import StageBinding, StageOutput
from pados.capability.artifacts import ArtifactRef
from pados.capability.model import VerificationMethod, descriptor_digest
from pados.capability.receipts import StageReceipt, VerificationStatus, parameters_hash
from pados.execution.context import canonical_hash
from pados.packs.algorithms.contracts import (
    DATASET_KIND,
    JSON_MEDIA,
    RECORD_KIND,
    RESULT_KIND,
    AlgorithmError,
    ResultEnvelope,
    canonical_json,
    dataset_hash,
    load_record,
    parse_dataset,
    sha256_hex,
)
from pados.packs.algorithms.meter import optional_deadline
from pados.packs.algorithms.registry import ALGORITHMS, adapter_descriptor

VERIFIER_ID = "algorithms.replay"


class _Unestablished(Enum):
    SEED = "SEED_UNKNOWN"


SEED_UNKNOWN: Final = _Unestablished.SEED  # the caller did not establish the trusted request seed


@dataclass(frozen=True)
class ExpectedStage:
    """What the trusted caller bound and observed for the stage being replayed (never read from the record).

    ``seed`` is the trusted request seed. It defaults to ``SEED_UNKNOWN`` (keeps positional construction
    compatible) and replay then fails closed with ``REPLAY_BINDING_SEED_UNKNOWN``; the two constructors
    always establish it."""

    capability_id: str
    capability_digest: str
    adapter_id: str
    adapter_version: str
    adapter_digest: str
    request_parameters_hash: str
    inputs: tuple[ArtifactRef, ...]
    outputs: tuple[ArtifactRef, ...]
    seed: int | None | _Unestablished = SEED_UNKNOWN

    @classmethod
    def from_binding(cls, binding: StageBinding, output: StageOutput) -> ExpectedStage:
        """From the executor's own binding (digests computed by ``bind_stage``, seed from the bound request)
        and the observed output."""
        return cls(binding.capability.capability_id, binding.capability_digest, binding.adapter.adapter_id,
                   binding.adapter.version, binding.adapter_digest, parameters_hash(binding.request.parameters),
                   tuple(binding.request.inputs), tuple(output.outputs), seed=binding.request.seed)

    @classmethod
    def from_receipt(cls, receipt: StageReceipt) -> ExpectedStage:
        """From an existing receipt, e.g. when re-verifying a stored run. The seed is the receipt's own
        ``seed`` field, whose semantics the receipt validators bind (a SEEDED stage must carry one)."""
        return cls(receipt.capability_id, receipt.capability_digest, receipt.adapter_id, receipt.adapter_version,
                   receipt.adapter_digest, receipt.parameters_hash, tuple(receipt.inputs), tuple(receipt.outputs),
                   seed=receipt.seed)


@dataclass(frozen=True)
class ReplayVerification:
    status: VerificationStatus
    code: str
    evidence_hash: str
    verifier_id: str = VERIFIER_ID
    method: VerificationMethod = VerificationMethod.DETERMINISTIC_REPLAY

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED


class _Mismatch(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _verdict(code: str, record_hash: str, recomputed: str | None) -> ReplayVerification:
    status = VerificationStatus.PASSED if code == "REPLAY_MATCH" else VerificationStatus.FAILED
    evidence = canonical_hash({"verifier_id": VERIFIER_ID, "method": VerificationMethod.DETERMINISTIC_REPLAY.value,
                               "code": code, "status": status.value, "record_sha256": record_hash,
                               "recomputed_result_hash": recomputed})
    return ReplayVerification(status, code, evidence)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise _Mismatch(code)


def _same_ref(ref: ArtifactRef, payload: bytes, kind: str) -> bool:
    return (ref.kind == kind and ref.media_type == JSON_MEDIA and ref.sha256 == sha256_hex(payload)
            and ref.size_bytes == len(payload))


def _request_hash(parameters: Mapping[str, Any]) -> str:
    try:
        return parameters_hash(parameters)
    except (TypeError, ValueError, RecursionError):
        pass
    raise _Mismatch("REPLAY_PARAMETERS_INVALID")


def _replay(expected: ExpectedStage, record_bytes: bytes, result_bytes: bytes, dataset_bytes: bytes,
            parameters: Mapping[str, Any], deadline_seconds: float | None) -> tuple[str, str]:
    """(recomputed result hash, recorded result hash) once every check passed; raises ``_Mismatch``."""
    record = load_record(record_bytes)
    spec = ALGORITHMS.get(record.algorithm_id)
    if spec is None or spec.compute is None:
        raise _Mismatch("REPLAY_ALGORITHM_UNKNOWN")
    _require(spec.algorithm_version == record.algorithm_version, "REPLAY_ALGORITHM_VERSION_MISMATCH")
    capability = spec.capability
    adapter = adapter_descriptor(spec)
    # The record's own claims.
    _require(record.capability_id == capability.capability_id, "REPLAY_RECORD_CAPABILITY_MISMATCH")
    _require(record.adapter_id == adapter.adapter_id, "REPLAY_RECORD_ADAPTER_MISMATCH")
    _require(record.seed is None, "REPLAY_SEED_NOT_APPLICABLE")  # execution refuses every non-null seed
    request_hash = _request_hash(parameters)
    _require(record.request_parameters_hash == request_hash, "REPLAY_RECORD_REQUEST_PARAMETERS_HASH_MISMATCH")
    _require(sha256_hex(result_bytes) == record.result_hash, "REPLAY_RESULT_HASH_MISMATCH")
    _require(sha256_hex(dataset_bytes) == record.dataset_artifact_sha256, "REPLAY_DATASET_ARTIFACT_MISMATCH")
    # The trusted caller's binding.
    _require(expected.capability_id == capability.capability_id, "REPLAY_BINDING_CAPABILITY_MISMATCH")
    _require(expected.capability_digest == descriptor_digest(capability),
             "REPLAY_BINDING_CAPABILITY_DIGEST_MISMATCH")
    _require(expected.adapter_id == adapter.adapter_id, "REPLAY_BINDING_ADAPTER_MISMATCH")
    _require(expected.adapter_version == adapter.version, "REPLAY_BINDING_ADAPTER_VERSION_MISMATCH")
    _require(expected.adapter_digest == descriptor_digest(adapter), "REPLAY_BINDING_ADAPTER_DIGEST_MISMATCH")
    _require(expected.request_parameters_hash == request_hash, "REPLAY_BINDING_PARAMETERS_HASH_MISMATCH")
    _require(expected.seed is not SEED_UNKNOWN, "REPLAY_BINDING_SEED_UNKNOWN")  # fail closed
    _require(expected.seed is None, "REPLAY_BINDING_SEED_NOT_APPLICABLE")  # the request execution would refuse
    _require(len(expected.inputs) == 1 and _same_ref(expected.inputs[0], dataset_bytes, DATASET_KIND),
             "REPLAY_BINDING_INPUT_MISMATCH")
    _require(len(expected.outputs) == 2 and _same_ref(expected.outputs[0], result_bytes, RESULT_KIND)
             and _same_ref(expected.outputs[1], record_bytes, RECORD_KIND), "REPLAY_BINDING_OUTPUT_MISMATCH")
    # Recomputation with the current implementation.
    dataset = parse_dataset(dataset_bytes)
    _require(dataset_hash(dataset) == record.dataset_hash, "REPLAY_DATASET_HASH_MISMATCH")
    normalized = spec.parse_parameters(parameters)
    _require(spec.parameters_hash(normalized) == record.parameters_hash, "REPLAY_PARAMETERS_HASH_MISMATCH")
    result, metrics = spec.run(dataset, normalized, deadline=optional_deadline(deadline_seconds))
    _require(dict(metrics) == record.metrics, "REPLAY_METRICS_MISMATCH")
    envelope = ResultEnvelope(
        algorithm_id=spec.algorithm_id, algorithm_version=spec.algorithm_version,
        capability_id=capability.capability_id, dataset_hash=record.dataset_hash,
        parameters_hash=record.parameters_hash, seed=None,
        parameters=normalized.model_dump(mode="json"), result=result, metrics=metrics)
    return sha256_hex(canonical_json(envelope.canonical())), record.result_hash


def verify_replay(expected: ExpectedStage, record_bytes: bytes, result_bytes: bytes, dataset_bytes: bytes,
                  parameters: Mapping[str, Any], *, deadline_seconds: float | None = None) -> ReplayVerification:
    """Replay under the caller's binding; always returns a verdict (never raises)."""
    record_hash = sha256_hex(record_bytes)
    recomputed: str | None = None
    try:
        recomputed, recorded = _replay(expected, record_bytes, result_bytes, dataset_bytes, parameters,
                                       deadline_seconds)
        code = "REPLAY_MATCH" if recomputed == recorded else "REPLAY_DIVERGED"
    except _Mismatch as exc:
        code = exc.code
    except AlgorithmError as exc:
        code = exc.code if exc.code.startswith("REPLAY_") else f"REPLAY_{exc.code}"
    except Exception:  # noqa: BLE001 - replay is a verdict boundary: no exception (or its content) escapes
        code = "REPLAY_UNEXPECTED_ERROR"
    return _verdict(code, record_hash, recomputed)
