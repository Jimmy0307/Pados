# SPDX-License-Identifier: AGPL-3.0-only
"""Model registry candidates and the human promotion gate.

    CANDIDATE -> PROMOTION_REQUESTED -> PROMOTED | REJECTED   (CANDIDATE -> REJECTED as well)

A ``ModelRegistryCandidate`` binds the model artifact hash, training metrics, held-out evaluation reports
and complete provenance (dataset hash, config hash, contract hash, base model, backend, code version,
seed, compute resource, licenses, job id) into one ``candidate_hash``.

Registration trusts nothing the caller presents (CR-12 TP-03/TP-04): it re-validates the full
contract-to-snapshot binding (snapshot hash including metadata) and the designated evaluation snapshot,
takes the backend license from the trusted backend registry, re-checks every license constraint, requires a
completed job whose retained run approval still verifies, re-derives provenance, checks that the artifact
is the one the contract produced, and RECOMPUTES the evaluation through the trusted evaluator (ALGORITHM_FIT
only; other kinds are ``PROMOTION_EVALUATION_UNVERIFIABLE`` in V1).

``PromotionGate`` is the only path to PROMOTED or REJECTED, with a signed, single-use human decision bound
to the exact registry entry: ``decision_subject`` hashes the candidate hash, the candidate id and the
candidate's REGISTERED event hash (CR-12b TP-01a), so a decision cannot be transplanted to another
candidate with the same artifact, nor to the same candidate re-registered after a rollback. The full signed
record is retained in the registry entry and in the consumed ledger; ``verified_state`` re-verifies its
MAC, operation, subject, approver and window and reports ``UNVERIFIED`` for any decided entry that does not
verify (CR-12 TP-01), so forging a promotion requires the approver's key. Decisions are idempotent per
(decision id, registry entry); a decision id already named by the registry or the job log is refused for
any other transition.

Rollback (CR-12b TP-01b, mandatory since CR-12c TP-01): registering, promoting and rejecting require an
operator ``ExternalAnchor`` on the registry, job and decision logs (``ANCHOR_REQUIRED``), and every
authority-bearing read (``verified_state``, ``descriptor_draft``) requires each of the three logs to sit
exactly at its anchored head. A coordinated rollback of every log, including the restore of an
already-consumed historical promotion, therefore fails closed with ``LEDGER_ROLLBACK_DETECTED`` (or
``LEDGER_HEAD_NOT_ANCHORED``). Over unanchored logs ``verified_state`` still reports for read-only use,
says ``UNANCHORED`` and reports every decided entry as ``UNVERIFIED``.

A PROMOTED candidate is still NOT a runtime adapter: ``descriptor_draft`` yields an UNREGISTERED,
UNAUTHORIZED adapter descriptor draft that the operator must register and a project grant must authorize
(installed != registered != authorized).
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.capability.artifacts import ArtifactRef, ArtifactStore
from pados.capability.model import AdapterDescriptor, Determinism, Distribution, LicenseInfo, LifecycleState
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.lifecycle.retention import DataKind
from pados.training._chain import (
    ANCHOR_REQUIRED,
    REGISTRY_LOG_NAME,
    Anchoring,
    ChainedLogError,
    ExternalAnchor,
    HashChainedLog,
)
from pados.training.backends import (
    TrainingBackend,
    TrainingBackendError,
    TrainingRunResult,
    parse_model,
    resolve_backend,
)
from pados.training.contracts import (
    DatasetSnapshot,
    ModelRef,
    TrainingContract,
    TrainingContractError,
    TrainingKind,
    check_contract,
    check_snapshot,
    validate_contract,
)
from pados.training.decisions import (
    DecisionBinding,
    DecisionError,
    DecisionOperation,
    DecisionVerifier,
    parse_time,
    require_verifier,
    retained_record,
)
from pados.training.evaluation import EvaluationReport, evaluate_algorithm_fit, metric_pairs
from pados.training.jobs import JobState, TrainingJob, TrainingJobError, TrainingJobs

CANDIDATE_SCHEMA = "pados.training.registry-candidate.v1"
REGISTRY_DATA_KIND = DataKind.MODEL_REGISTRY_LEDGER
DECISION_SUBJECT_SCHEMA = "pados.training.registry-decision-subject/v1"
REGISTRY_FIELDS = ("seq", "candidate_hash", "candidate_id", "event", "from_state", "to_state", "artifact_sha256",
                   "provenance_hash", "evaluation_count", "decision_id", "decision", "actor", "at", "prev_hash",
                   "entry_hash")
MAX_EVALUATIONS = 16
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRINCIPAL = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")


class CandidateState(StrEnum):
    CANDIDATE = "CANDIDATE"
    PROMOTION_REQUESTED = "PROMOTION_REQUESTED"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    UNVERIFIED = "UNVERIFIED"  # never recorded: verified_state reports a decided entry that does not verify


class RegistryEvent(StrEnum):
    REGISTERED = "REGISTERED"
    PROMOTION_REQUESTED = "PROMOTION_REQUESTED"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


_EVENT_TARGET = {
    RegistryEvent.REGISTERED: CandidateState.CANDIDATE,
    RegistryEvent.PROMOTION_REQUESTED: CandidateState.PROMOTION_REQUESTED,
    RegistryEvent.PROMOTED: CandidateState.PROMOTED,
    RegistryEvent.REJECTED: CandidateState.REJECTED,
}
TRANSITIONS: dict[CandidateState, frozenset[CandidateState]] = {
    CandidateState.CANDIDATE: frozenset({CandidateState.PROMOTION_REQUESTED, CandidateState.REJECTED}),
    CandidateState.PROMOTION_REQUESTED: frozenset({CandidateState.PROMOTED, CandidateState.REJECTED}),
    CandidateState.PROMOTED: frozenset(),
    CandidateState.REJECTED: frozenset(),
    CandidateState.UNVERIFIED: frozenset(),
}
_DECIDED = {CandidateState.PROMOTED: DecisionOperation.PROMOTE_MODEL,
            CandidateState.REJECTED: DecisionOperation.REJECT_MODEL}


class RegistryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Provenance(BaseModel):
    """Everything needed to reproduce and license-check the artifact. All fields are required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    contract_id: str
    contract_hash: str
    kind: TrainingKind
    dataset_snapshot_id: str
    dataset_hash: str
    dataset_snapshot_hash: str
    dataset_classification: DataClassification
    dataset_license_spdx: str
    config_hash: str
    base_model: ModelRef | None
    backend_adapter_id: str
    backend_license_spdx: str
    code_version: str
    seed: int = Field(ge=0, le=2**32 - 1)
    compute_resource_ref: str
    output_license_spdx: str

    @field_validator("job_id", "contract_id", "dataset_snapshot_id", "compute_resource_ref", "backend_adapter_id")
    @classmethod
    def identifiers(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("PROVENANCE_INCOMPLETE")
        return value

    @field_validator("contract_hash", "dataset_hash", "dataset_snapshot_hash", "config_hash")
    @classmethod
    def hashes(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("PROVENANCE_INCOMPLETE")
        return value

    @field_validator("code_version")
    @classmethod
    def commit(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            raise ValueError("PROVENANCE_INCOMPLETE")
        return value

    @field_validator("dataset_license_spdx", "backend_license_spdx", "output_license_spdx")
    @classmethod
    def licenses(cls, value: str) -> str:
        if not value or len(value) > 200:
            raise ValueError("PROVENANCE_INCOMPLETE")
        return value

    @model_validator(mode="after")
    def base_model_for_kind(self) -> Provenance:
        if (self.base_model is None) != (self.kind is TrainingKind.ALGORITHM_FIT):
            raise ValueError("PROVENANCE_INCOMPLETE")
        return self

    @classmethod
    def derive(cls, *, job_id: str, contract: TrainingContract, dataset: DatasetSnapshot,
               backend_license_spdx: str) -> Provenance:
        return cls(job_id=job_id, contract_id=contract.contract_id, contract_hash=contract.contract_hash,
                   kind=contract.kind, dataset_snapshot_id=dataset.snapshot_id, dataset_hash=dataset.content_hash,
                   dataset_snapshot_hash=dataset.snapshot_hash, dataset_classification=dataset.classification,
                   dataset_license_spdx=dataset.license_spdx, config_hash=contract.config_hash,
                   base_model=contract.base_model, backend_adapter_id=contract.backend_adapter_id,
                   backend_license_spdx=backend_license_spdx, code_version=contract.code_version,
                   seed=contract.seed, compute_resource_ref=contract.compute_resource_ref,
                   output_license_spdx=contract.license_constraints.output_spdx)


class ModelRegistryCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CANDIDATE_SCHEMA
    candidate_id: str
    artifact: ArtifactRef
    metrics: tuple[tuple[str, float], ...] = Field(max_length=32)
    evaluations: tuple[EvaluationReport, ...] = Field(default=(), max_length=MAX_EVALUATIONS)
    provenance: Provenance
    semantic_authority: str = "NONE"

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != CANDIDATE_SCHEMA:
            raise ValueError("CANDIDATE_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("candidate_id")
    @classmethod
    def identifier(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("CANDIDATE_ID_INVALID")
        return value

    @field_validator("metrics", mode="before")
    @classmethod
    def sorted_metrics(cls, value: Any) -> tuple[tuple[str, float], ...]:
        return metric_pairs(value)

    @field_validator("semantic_authority")
    @classmethod
    def no_authority(cls, value: str) -> str:
        if value != "NONE":
            raise ValueError("CANDIDATE_CANNOT_CARRY_AUTHORITY")
        return value

    @model_validator(mode="after")
    def evaluations_bound(self) -> ModelRegistryCandidate:
        for report in self.evaluations:
            if (report.model_artifact_sha256 != self.artifact.sha256
                    or report.training_dataset_hash != self.provenance.dataset_hash):
                raise ValueError("EVALUATION_NOT_FOR_CANDIDATE")
        if len({r.report_hash for r in self.evaluations}) != len(self.evaluations):
            raise ValueError("EVALUATION_DUPLICATE")
        return self

    @property
    def candidate_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    @property
    def provenance_hash(self) -> str:
        return canonical_hash(self.provenance.model_dump(mode="json"))


def _checked(candidate: ModelRegistryCandidate) -> ModelRegistryCandidate:
    try:
        return ModelRegistryCandidate.model_validate(candidate.model_dump(mode="json"))
    except ValueError as exc:
        raise RegistryError("CANDIDATE_INTEGRITY_INVALID") from exc


def _evaluate(candidate_artifact: ArtifactRef, *, store: ArtifactStore, contract: TrainingContract,
              dataset: DatasetSnapshot, eval_dataset: DatasetSnapshot, created_at: str,
              report_id: str) -> EvaluationReport:
    """The trusted evaluator over the contract's designated held-out snapshot (ALGORITHM_FIT only)."""
    if contract.kind is not TrainingKind.ALGORITHM_FIT:
        raise RegistryError("PROMOTION_EVALUATION_UNVERIFIABLE")
    if contract.eval_dataset is None or not contract.eval_dataset.matches(eval_dataset):
        raise RegistryError("EVALUATION_DATASET_NOT_DESIGNATED")
    try:
        return evaluate_algorithm_fit(candidate_artifact, store=store, eval_dataset=eval_dataset,
                                      training_dataset=dataset, report_id=report_id, created_at=created_at)
    except (TrainingContractError, TrainingBackendError) as exc:
        raise RegistryError(exc.code) from None


def build_candidate(*, candidate_id: str, job: TrainingJob, result: TrainingRunResult, contract: TrainingContract,
                    dataset: DatasetSnapshot, backend: TrainingBackend, store: ArtifactStore,
                    eval_dataset: DatasetSnapshot | None = None, created_at: str) -> ModelRegistryCandidate:
    """Assemble a candidate; its evaluation (if any) is computed here by the trusted evaluator, never passed in."""
    contract, dataset = check_contract(contract), check_snapshot(dataset)
    if job.state is not JobState.COMPLETED or job.artifact_sha256 != result.artifact.ref.sha256:
        raise RegistryError("JOB_NOT_COMPLETED_FOR_ARTIFACT")
    if contract.kind is TrainingKind.EVALUATION_ONLY:
        raise RegistryError("EVALUATION_ONLY_PRODUCES_NO_CANDIDATE")
    evaluations: tuple[EvaluationReport, ...] = ()
    if eval_dataset is not None:
        evaluations = (_evaluate(result.artifact.ref, store=store, contract=contract, dataset=dataset,
                                 eval_dataset=eval_dataset, created_at=created_at, report_id=f"eval-{candidate_id}"),)
    return ModelRegistryCandidate(candidate_id=candidate_id, artifact=result.artifact.ref,
                                  metrics=metric_pairs(result.metrics), evaluations=evaluations,
                                  provenance=Provenance.derive(job_id=job.job_id, contract=contract, dataset=dataset,
                                                               backend_license_spdx=backend.descriptor.license.spdx))


@dataclass(frozen=True)
class RegistryRecord:
    candidate_hash: str
    candidate_id: str
    state: CandidateState
    artifact_sha256: str
    provenance_hash: str
    evaluation_count: int
    decision_id: str | None
    decision: Mapping[str, str] | None
    actor: str
    at: str
    head_hash: str
    registered_hash: str


def decision_subject(record: RegistryRecord) -> str:
    """The subject a PROMOTE_MODEL / REJECT_MODEL decision signs: this exact registry entry."""
    return canonical_hash({"schema": DECISION_SUBJECT_SCHEMA, "candidate_hash": record.candidate_hash,
                           "candidate_id": record.candidate_id, "registered_hash": record.registered_hash})


@dataclass(frozen=True)
class GateVerdict:
    """A re-verified registry state plus whether rollback below an external anchor would have been detected."""

    state: CandidateState
    anchoring: Anchoring


def _replay(entries: list[dict[str, Any]]) -> dict[str, RegistryRecord]:
    records: dict[str, RegistryRecord] = {}
    for entry in entries:
        try:
            event = RegistryEvent(entry["event"])
        except ValueError:
            raise RegistryError("REGISTRY_LOG_CORRUPT") from None
        target = _EVENT_TARGET[event]
        current = records.get(entry["candidate_hash"])
        decision = entry["decision"]
        decided = target in _DECIDED
        if entry["to_state"] != target.value or decided != (decision is not None):
            raise RegistryError("REGISTRY_LOG_CORRUPT")
        if decided and not (isinstance(decision, dict) and entry["decision_id"]
                            and decision.get("decision_id") == entry["decision_id"]):
            raise RegistryError("REGISTRY_LOG_CORRUPT")
        if event is RegistryEvent.REGISTERED:
            if current is not None or entry["from_state"] is not None:
                raise RegistryError("REGISTRY_LOG_CORRUPT")
            records[entry["candidate_hash"]] = RegistryRecord(
                entry["candidate_hash"], entry["candidate_id"], target, entry["artifact_sha256"],
                entry["provenance_hash"], entry["evaluation_count"], None, None, entry["actor"], entry["at"],
                entry["entry_hash"], entry["entry_hash"])
            continue
        if (current is None or entry["from_state"] != current.state.value
                or target not in TRANSITIONS[current.state]
                or entry["artifact_sha256"] != current.artifact_sha256):
            raise RegistryError("REGISTRY_LOG_CORRUPT")
        records[entry["candidate_hash"]] = RegistryRecord(
            current.candidate_hash, current.candidate_id, target, current.artifact_sha256, current.provenance_hash,
            current.evaluation_count, entry["decision_id"] or current.decision_id,
            decision if decided else current.decision, entry["actor"], entry["at"], entry["entry_hash"],
            current.registered_hash)
    return records


def _logged_decision_ids(entries: list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    for entry in entries:
        if entry["decision_id"]:
            found.add(entry["decision_id"])
        if isinstance(entry["decision"], dict):
            found.add(str(entry["decision"].get("decision_id")))
    return found


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ModelRegistry:
    """Candidate states over one append-only, hash-chained ledger. Registering is not authorizing.

    ``backends`` is the trusted, operator-configured backend registry (license and kind come from it, never
    from the caller); ``store`` is the trusted executor-side artifact store used to recompute evaluations.
    """

    def __init__(self, log_path: Path, *, jobs: TrainingJobs, verifier: DecisionVerifier,
                 backends: Mapping[str, TrainingBackend], store: ArtifactStore,
                 clock: Callable[[], datetime] = _utc_now, anchor: ExternalAnchor | None = None,
                 log_name: str = REGISTRY_LOG_NAME) -> None:
        self._verifier = require_verifier(verifier)
        self._log = HashChainedLog(log_path, fields=REGISTRY_FIELDS, corrupt_code="REGISTRY_LOG_CORRUPT",
                                   log_name=log_name, anchor=anchor)
        self._jobs = jobs
        self._backends = dict(backends)
        self._store = store
        self._clock = clock

    def _records(self) -> dict[str, RegistryRecord]:
        try:
            return _replay(self._log.entries())
        except ChainedLogError as exc:
            raise RegistryError(exc.code) from None

    def record(self, candidate_hash: str) -> RegistryRecord:
        found = self._records().get(candidate_hash)
        if found is None:
            raise RegistryError("CANDIDATE_NOT_REGISTERED")
        return found

    @property
    def anchoring(self) -> Anchoring:
        """ANCHORED only when the registry log, the job log and the consumed ledger are all anchored."""
        if self._log.anchoring is Anchoring.ANCHORED and self._jobs.anchoring is Anchoring.ANCHORED:
            return Anchoring.ANCHORED
        return Anchoring.UNANCHORED

    def _require_anchored(self) -> None:
        if self.anchoring is not Anchoring.ANCHORED:
            raise RegistryError(ANCHOR_REQUIRED)

    def verify_heads(self) -> None:
        """The registry log, the job log and the consumed ledger all sit exactly at their anchored heads."""
        self._require_anchored()
        self._records()
        try:
            self._jobs.verify_heads()
        except TrainingJobError as exc:
            raise RegistryError(exc.code) from None

    def decision_subject(self, candidate_hash: str) -> str:
        """What the operator signs to promote or reject this registered entry (operator tool convenience)."""
        return decision_subject(self.record(candidate_hash))

    def state(self, candidate_hash: str) -> CandidateState:
        """Ledger state as recorded; ``PromotionGate.verified_state`` re-verifies decided states."""
        return self.record(candidate_hash).state

    def used_decision_ids(self) -> frozenset[str]:
        try:
            return frozenset(_logged_decision_ids(self._log.entries()))
        except ChainedLogError as exc:
            raise RegistryError(exc.code) from None

    def _now(self) -> str:
        return self._clock().astimezone(UTC).isoformat()

    def _entry(self, candidate: ModelRegistryCandidate, event: RegistryEvent, *, from_state: CandidateState | None,
               actor: str, decision: Mapping[str, str] | None = None) -> dict[str, Any]:
        return {"candidate_hash": candidate.candidate_hash, "candidate_id": candidate.candidate_id,
                "event": event.value, "from_state": None if from_state is None else from_state.value,
                "to_state": _EVENT_TARGET[event].value, "artifact_sha256": candidate.artifact.sha256,
                "provenance_hash": candidate.provenance_hash, "evaluation_count": len(candidate.evaluations),
                "decision_id": None if decision is None else decision["decision_id"],
                "decision": None if decision is None else dict(decision), "actor": actor, "at": self._now()}

    def _append(self, build: Callable[[list[dict[str, Any]]], dict[str, Any] | None]) -> None:
        try:
            self._log.append(build)
        except ChainedLogError as exc:
            raise RegistryError(exc.code) from None

    def _verify_registration(self, candidate: ModelRegistryCandidate, contract: TrainingContract,
                             dataset: DatasetSnapshot, eval_dataset: DatasetSnapshot | None) -> None:
        job = self._jobs.get(candidate.provenance.job_id)
        if job.state is not JobState.COMPLETED or job.artifact_sha256 != candidate.artifact.sha256:
            raise RegistryError("JOB_NOT_COMPLETED_FOR_ARTIFACT")
        if not self._jobs.approval_verified(job):
            raise RegistryError("JOB_RUN_APPROVAL_UNVERIFIED")
        if job.metrics_hash != canonical_hash(dict(candidate.metrics)):
            raise RegistryError("CANDIDATE_METRICS_NOT_FROM_JOB")
        try:
            contract = check_contract(contract)
            if contract.contract_hash != job.contract_hash:
                raise RegistryError("PROVENANCE_MISMATCH")
            backend = resolve_backend(self._backends, contract)
            license_spdx = backend.descriptor.license.spdx
            # Full binding: snapshot id + content hash + snapshot hash (metadata), eval binding, licenses.
            validate_contract(contract, dataset, backend_kind=backend.kind,
                              backend_adapter_id=backend.descriptor.adapter_id, backend_license_spdx=license_spdx,
                              eval_dataset=eval_dataset)
        except TrainingContractError as exc:
            raise RegistryError(exc.code) from None
        dataset = check_snapshot(dataset)
        if dataset.content_hash != job.dataset_hash:
            raise RegistryError("PROVENANCE_MISMATCH")
        if contract.kind is TrainingKind.EVALUATION_ONLY:
            raise RegistryError("EVALUATION_ONLY_PRODUCES_NO_CANDIDATE")
        expected = Provenance.derive(job_id=job.job_id, contract=contract, dataset=dataset,
                                     backend_license_spdx=license_spdx)
        if expected != candidate.provenance:
            raise RegistryError("PROVENANCE_MISMATCH")
        if contract.kind is TrainingKind.ALGORITHM_FIT:
            try:
                model = parse_model(self._store.get(candidate.artifact))
            except (TrainingBackendError, ValueError) as exc:
                raise RegistryError("ARTIFACT_UNREADABLE") from exc
            if model["contract_hash"] != contract.contract_hash or model["dataset_hash"] != dataset.content_hash:
                raise RegistryError("ARTIFACT_NOT_FROM_CONTRACT")
        if candidate.evaluations:
            if contract.kind is not TrainingKind.ALGORITHM_FIT:
                raise RegistryError("PROMOTION_EVALUATION_UNVERIFIABLE")
            if eval_dataset is None:
                raise RegistryError("EVALUATION_DATASET_NOT_DESIGNATED")
            trusted = _evaluate(candidate.artifact, store=self._store, contract=contract, dataset=dataset,
                                eval_dataset=eval_dataset, created_at=self._now(), report_id="registry-recompute")
            evidence = [report.evidence_hash for report in candidate.evaluations]
            if len(set(evidence)) != len(evidence):
                raise RegistryError("EVALUATION_DUPLICATE")
            if any(item != trusted.evidence_hash for item in evidence):
                raise RegistryError("EVALUATION_NOT_REPRODUCED")

    def register(self, candidate: ModelRegistryCandidate, *, contract: TrainingContract, dataset: DatasetSnapshot,
                 eval_dataset: DatasetSnapshot | None = None, registered_by: str) -> CandidateState:
        """Register a CANDIDATE after re-verifying everything it claims (see module docstring)."""
        self._require_anchored()
        candidate = _checked(candidate)
        if not _PRINCIPAL.fullmatch(registered_by):
            raise RegistryError("ACTOR_INVALID")
        try:
            self._verify_registration(candidate, contract, dataset, eval_dataset)
        except TrainingJobError as exc:
            raise RegistryError(exc.code) from None

        def build(entries: list[dict[str, Any]]) -> dict[str, Any]:
            if candidate.candidate_hash in _replay(entries):
                raise RegistryError("CANDIDATE_ALREADY_REGISTERED")
            return self._entry(candidate, RegistryEvent.REGISTERED, from_state=None, actor=registered_by)

        self._append(build)
        return self.state(candidate.candidate_hash)

    def request_promotion(self, candidate: ModelRegistryCandidate, *, requested_by: str) -> CandidateState:
        """Ask for a human decision. Requires registry-verified held-out evaluation evidence; decides nothing."""
        candidate = _checked(candidate)
        if not _PRINCIPAL.fullmatch(requested_by):
            raise RegistryError("ACTOR_INVALID")
        if candidate.provenance.kind is not TrainingKind.ALGORITHM_FIT:
            raise RegistryError("PROMOTION_EVALUATION_UNVERIFIABLE")

        def build(entries: list[dict[str, Any]]) -> dict[str, Any]:
            current = _replay(entries).get(candidate.candidate_hash)
            if current is None:
                raise RegistryError("CANDIDATE_NOT_REGISTERED")
            if current.evaluation_count < 1 or not candidate.evaluations:
                raise RegistryError("PROMOTION_REQUIRES_EVALUATION")
            if CandidateState.PROMOTION_REQUESTED not in TRANSITIONS[current.state]:
                raise RegistryError("CANDIDATE_TRANSITION_INVALID")
            return self._entry(candidate, RegistryEvent.PROMOTION_REQUESTED, from_state=current.state,
                               actor=requested_by)

        self._append(build)
        return self.state(candidate.candidate_hash)

    def _apply_decision(self, candidate: ModelRegistryCandidate, record: Mapping[str, Any]) -> CandidateState:
        operation = record.get("operation") if isinstance(record, Mapping) else None
        if operation == DecisionOperation.PROMOTE_MODEL.value:
            target, event = CandidateState.PROMOTED, RegistryEvent.PROMOTED
        elif operation == DecisionOperation.REJECT_MODEL.value:
            target, event = CandidateState.REJECTED, RegistryEvent.REJECTED
        else:
            raise RegistryError("PROMOTION_DECISION_OPERATION_INVALID")
        self._require_anchored()
        try:
            retained = retained_record(record)
        except DecisionError as error:
            raise RegistryError(f"PROMOTION_DECISION_REFUSED:{error.code.value}") from None

        def build(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
            records = _replay(entries)
            current = records.get(candidate.candidate_hash)
            if current is None:
                raise RegistryError("CANDIDATE_NOT_REGISTERED")
            binding = DecisionBinding(_DECIDED[target], decision_subject(current))
            if current.state is target and current.decision == retained:
                return None  # the same decision replayed for the same candidate: nothing changes
            if target not in TRANSITIONS[current.state]:
                raise RegistryError("CANDIDATE_TRANSITION_INVALID")
            try:
                used = _logged_decision_ids(entries) | self._jobs.used_decision_ids()
            except TrainingJobError as exc:
                raise RegistryError(exc.code) from None
            if retained["decision_id"] in used:
                raise RegistryError("DECISION_ALREADY_USED")
            # Consumed only after the state checks, so a refused move burns no decision.
            outcome = self._verifier.consume(retained, binding, now=self._clock())
            if not outcome.ok or outcome.approver is None:
                raise RegistryError(f"PROMOTION_DECISION_REFUSED:{outcome.code.value}")
            return self._entry(candidate, event, from_state=current.state, actor=outcome.approver, decision=retained)

        self._append(build)
        return self.state(candidate.candidate_hash)

    def _verified(self, candidate: ModelRegistryCandidate) -> tuple[CandidateState, RegistryRecord]:
        anchored = self.anchoring is Anchoring.ANCHORED
        if anchored:  # every log at its anchored head: a restored or rolled-back history fails closed here
            self.verify_heads()
        record = self.record(candidate.candidate_hash)
        if record.state not in _DECIDED:
            return record.state, record
        if not anchored:  # decisions are only ever applied over anchored logs (CR-12c TP-01)
            return CandidateState.UNVERIFIED, record
        binding = DecisionBinding(_DECIDED[record.state], decision_subject(record))
        decision = None if record.decision is None else dict(record.decision)
        if decision is None:
            return CandidateState.UNVERIFIED, record
        try:
            at = parse_time(record.at)
        except ValueError:
            return CandidateState.UNVERIFIED, record
        outcome = self._verifier.verify(decision, binding, at=at)
        try:
            consumed = self._verifier.consumed_matches(decision, binding)
        except DecisionError as error:  # rollback below the anchored ledger head: fail closed, loudly
            raise RegistryError(error.code.value) from None
        if (not outcome.ok or outcome.decision_id != record.decision_id or outcome.approver != record.actor
                or not consumed):
            return CandidateState.UNVERIFIED, record
        return record.state, record


class PromotionGate:
    """The only way to PROMOTED or REJECTED: a signed human decision bound to the exact candidate hash."""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    def decide(self, candidate: ModelRegistryCandidate, record: Mapping[str, Any]) -> CandidateState:
        """Consume a signed PROMOTE_MODEL / REJECT_MODEL decision for exactly this candidate hash."""
        return self._registry._apply_decision(_checked(candidate), record)

    @property
    def anchoring(self) -> Anchoring:
        return self._registry.anchoring

    def verified_state(self, candidate: ModelRegistryCandidate) -> GateVerdict:
        """Registry state with every decided entry re-verified from its retained signed decision, plus
        ``ANCHORED`` / ``UNANCHORED``. ``UNVERIFIED`` when the retained decision does not verify (forged,
        altered, transplanted or not consumed) or the logs are unanchored (read-only reporting);
        ``LEDGER_ROLLBACK_DETECTED`` / ``LEDGER_HEAD_NOT_ANCHORED`` when any of the three logs is not at its
        anchored head."""
        return GateVerdict(self._registry._verified(_checked(candidate))[0], self._registry.anchoring)

    def descriptor_draft(self, candidate: ModelRegistryCandidate, *, adapter_id: str, capability_id: str,
                         version: str) -> ModelDescriptorDraft:
        self._registry._require_anchored()
        candidate = _checked(candidate)
        state, record = self._registry._verified(candidate)
        if state is CandidateState.UNVERIFIED:
            raise RegistryError("DECISION_UNVERIFIED")
        if state is not CandidateState.PROMOTED or record.decision_id is None:
            raise RegistryError("CANDIDATE_NOT_PROMOTED")
        provenance = candidate.provenance
        language_model = provenance.kind is TrainingKind.LORA_FINETUNE
        proposed = AdapterDescriptor(
            adapter_id=adapter_id, capability_id=capability_id, version=version,
            backend=provenance.backend_adapter_id[:40],
            determinism=Determinism.DETERMINISTIC if provenance.kind is TrainingKind.ALGORITHM_FIT
            else Determinism.PROBABILISTIC,
            max_classification=provenance.dataset_classification, network_requirement=NetworkRequirement.LOCAL_ONLY,
            license=LicenseInfo(spdx=provenance.output_license_spdx, engine_version="n/a",
                                distribution=Distribution.NONE),
            lifecycle=LifecycleState.EXPERIMENTAL, delegates_to_worker_router=language_model)
        return ModelDescriptorDraft(candidate_hash=candidate.candidate_hash, artifact_sha256=candidate.artifact.sha256,
                                    promotion_decision_id=record.decision_id, proposed=proposed,
                                    required_grant=proposed.grant_capability, anchoring=self._registry.anchoring)


@dataclass(frozen=True)
class ModelDescriptorDraft:
    """A proposal for the operator. Not installed, not registered, not authorized, not selectable."""

    candidate_hash: str
    artifact_sha256: str
    promotion_decision_id: str
    proposed: AdapterDescriptor
    required_grant: str
    anchoring: Anchoring = Anchoring.UNANCHORED
    registration_state: str = "UNREGISTERED"
    authorization_state: str = "UNAUTHORIZED"
    semantic_authority: str = "NONE"

    def __post_init__(self) -> None:
        if (self.registration_state != "UNREGISTERED" or self.authorization_state != "UNAUTHORIZED"
                or self.semantic_authority != "NONE"):
            raise ValueError("DESCRIPTOR_DRAFT_CANNOT_CARRY_AUTHORITY")
        if self.required_grant != self.proposed.grant_capability:
            raise ValueError("DESCRIPTOR_DRAFT_GRANT_MISMATCH")
