# SPDX-License-Identifier: AGPL-3.0-only
"""Training job state machine over an append-only, hash-chained event log.

    DRAFT -> APPROVED_TO_RUN -> RUNNING -> COMPLETED | FAILED | CANCELLED

The event log is the only job state: ``TrainingJobs`` replays it and refuses any illegal transition, so a
tampered or forged entry that tries to move a job anywhere else (including a registry or runtime state such
as PROMOTED, REGISTERED or DEPLOYED, which do not exist here) fails closed.

Approval to run is a signed, single-use human decision bound to ONE job: its subject is
``run_approval_subject(job)``, the canonical hash of the contract hash, the job id and the job's CREATED
event hash (CR-12b TP-01a), so an approval cannot be transplanted to another job that shares the contract,
nor to a job re-created under the same id after a rollback. The APPROVED event retains the full signed
record, and ``approval_verified`` / ``start`` re-verify its MAC, operation, subject, registered approver and
validity window, and that the consumed ledger holds the same record (CR-12 TP-01). Approving is idempotent
per (decision id, job) - replaying the same decision for the same job is a no-op - and a decision id already
named by the job log or the registry log is refused for any other job. Jobs receive a verify-only
``DecisionVerifier`` and can never sign (CR-12 TP-02).

Anchoring is mandatory for authority (CR-12c TP-01): approving and starting a run require an
``ExternalAnchor`` on the job log, the consumed-decision ledger and the registry log (``ANCHOR_REQUIRED``),
and every authority-bearing read requires each of those logs to sit exactly at its anchored head, so a
coordinated rollback fails closed (``LEDGER_ROLLBACK_DETECTED`` / ``LEDGER_HEAD_NOT_ANCHORED``). An
unanchored job log can still be read for reporting; its approvals never verify.

A completed job has produced a candidate artifact, nothing more: this module has no path to the model
registry, promotion, adapter registration or grants. Entries hold identifiers, hashes, states, timestamps
and the retained signed decision only (``assert_minimized``).
"""
from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.capability.artifacts import ArtifactStore, ScopedArtifacts
from pados.execution.context import canonical_hash
from pados.lifecycle.retention import DataKind
from pados.training._chain import (
    ANCHOR_REQUIRED,
    JOB_LOG_NAME,
    REGISTRY_LOG_NAME,
    Anchoring,
    ChainedLogError,
    ExternalAnchor,
    HashChainedLog,
    decision_ids,
)
from pados.training.backends import TrainingBackend, TrainingBackendError, TrainingRunResult
from pados.training.contracts import (
    DatasetSnapshot,
    TrainingContract,
    TrainingContractError,
    check_contract,
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

JOB_LOG_DATA_KIND = DataKind.TRAINING_JOB_EVENT_LOG
RUN_SUBJECT_SCHEMA = "pados.training.run-approval-subject/v1"
JOB_LOG_FIELDS = ("seq", "job_id", "event", "from_state", "to_state", "contract_hash", "kind",
                  "backend_adapter_id", "dataset_hash", "approval_id", "decision", "artifact_sha256",
                  "metrics_hash", "failure_code", "at", "prev_hash", "entry_hash")
MAX_MODEL_OUTPUT_BYTES = 16 * 1024 * 1024
_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{7,63}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class JobState(StrEnum):
    DRAFT = "DRAFT"
    APPROVED_TO_RUN = "APPROVED_TO_RUN"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobEvent(StrEnum):
    CREATED = "CREATED"
    APPROVED = "APPROVED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_EVENT_TARGET = {
    JobEvent.CREATED: JobState.DRAFT, JobEvent.APPROVED: JobState.APPROVED_TO_RUN,
    JobEvent.STARTED: JobState.RUNNING, JobEvent.COMPLETED: JobState.COMPLETED,
    JobEvent.FAILED: JobState.FAILED, JobEvent.CANCELLED: JobState.CANCELLED,
}
TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.DRAFT: frozenset({JobState.APPROVED_TO_RUN, JobState.CANCELLED}),
    JobState.APPROVED_TO_RUN: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


class TrainingJobError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def transition(current: JobState | str, target: JobState | str) -> JobState:
    """The only legal moves; anything outside the job vocabulary (e.g. PROMOTED, DEPLOYED) is refused."""
    try:
        source, destination = JobState(current), JobState(target)
    except ValueError:
        raise TrainingJobError("JOB_STATE_UNKNOWN") from None
    if destination not in TRANSITIONS[source]:
        raise TrainingJobError("JOB_TRANSITION_INVALID")
    return destination


@dataclass(frozen=True)
class TrainingJob:
    job_id: str
    contract_hash: str
    kind: str
    backend_adapter_id: str
    dataset_hash: str
    state: JobState
    created_at: str
    updated_at: str
    approval_id: str | None = None
    approval_decision: Mapping[str, str] | None = None
    approved_at: str | None = None
    artifact_sha256: str | None = None
    metrics_hash: str | None = None
    failure_code: str | None = None
    head_hash: str = ""
    created_hash: str = ""


def run_approval_subject(job: TrainingJob) -> str:
    """The subject an APPROVE_TRAINING_RUN decision signs: this exact job, not merely its contract."""
    if not job.created_hash:
        raise TrainingJobError("JOB_CREATION_UNKNOWN")
    return canonical_hash({"schema": RUN_SUBJECT_SCHEMA, "contract_hash": job.contract_hash, "job_id": job.job_id,
                           "created_hash": job.created_hash})


def _apply(jobs: dict[str, TrainingJob], entry: dict[str, Any]) -> None:
    try:
        event = JobEvent(entry["event"])
    except ValueError:
        raise TrainingJobError("JOB_LOG_CORRUPT") from None
    target = _EVENT_TARGET[event]
    if entry["to_state"] != target.value:
        raise TrainingJobError("JOB_LOG_CORRUPT")
    if (event is JobEvent.APPROVED) != (entry["decision"] is not None):
        raise TrainingJobError("JOB_LOG_CORRUPT")
    job_id = entry["job_id"]
    current = jobs.get(job_id)
    if event is JobEvent.CREATED:
        if current is not None or entry["from_state"] is not None:
            raise TrainingJobError("JOB_LOG_CORRUPT")
        jobs[job_id] = TrainingJob(job_id, entry["contract_hash"], entry["kind"], entry["backend_adapter_id"],
                                   entry["dataset_hash"], JobState.DRAFT, entry["at"], entry["at"],
                                   head_hash=entry["entry_hash"], created_hash=entry["entry_hash"])
        return
    if current is None or entry["from_state"] != current.state.value or entry["contract_hash"] != current.contract_hash:
        raise TrainingJobError("JOB_LOG_CORRUPT")
    try:
        transition(current.state, target)
    except TrainingJobError:
        raise TrainingJobError("JOB_LOG_CORRUPT") from None
    decision = entry["decision"]
    if event is JobEvent.APPROVED and not (
            isinstance(decision, dict) and entry["approval_id"] and decision.get("decision_id") == entry["approval_id"]):
        raise TrainingJobError("JOB_LOG_CORRUPT")
    if event is JobEvent.STARTED and entry["approval_id"] != current.approval_id:
        raise TrainingJobError("JOB_LOG_CORRUPT")
    if event is JobEvent.COMPLETED and not (entry["artifact_sha256"] and entry["metrics_hash"]):
        raise TrainingJobError("JOB_LOG_CORRUPT")
    approved = event is JobEvent.APPROVED
    jobs[job_id] = replace(current, state=target, updated_at=entry["at"], head_hash=entry["entry_hash"],
                           approval_id=entry["approval_id"] or current.approval_id,
                           approval_decision=decision if approved else current.approval_decision,
                           approved_at=entry["at"] if approved else current.approved_at,
                           artifact_sha256=entry["artifact_sha256"] or current.artifact_sha256,
                           metrics_hash=entry["metrics_hash"] or current.metrics_hash,
                           failure_code=entry["failure_code"] or current.failure_code)


def replay(entries: list[dict[str, Any]]) -> dict[str, TrainingJob]:
    jobs: dict[str, TrainingJob] = {}
    for entry in entries:
        _apply(jobs, entry)
    return jobs


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TrainingJobs:
    """Job lifecycle over one event log file (operator-configured path).

    ``registry_log`` names the model registry ledger so a decision id it already holds is refused here too;
    with ``anchor`` that log's head is checked against the head anchored under ``registry_log_name`` (the
    registry must use the same anchor). ``anchor`` records every job-log head externally and is required
    for approving and starting runs (see ``pados.training._chain``).
    """

    def __init__(self, log_path: Path, *, verifier: DecisionVerifier, clock: Callable[[], datetime] = _utc_now,
                 registry_log: Path | None = None, anchor: ExternalAnchor | None = None,
                 log_name: str = JOB_LOG_NAME, registry_log_name: str = REGISTRY_LOG_NAME) -> None:
        self._verifier = require_verifier(verifier)
        self._log = HashChainedLog(log_path, fields=JOB_LOG_FIELDS, corrupt_code="JOB_LOG_CORRUPT",
                                   log_name=log_name, anchor=anchor)
        self._clock = clock
        self._registry_log = registry_log
        self._registry_log_name = registry_log_name

    @property
    def anchoring(self) -> Anchoring:
        """ANCHORED only when the job log, the consumed-decision ledger and the registry log are all anchored."""
        if (self._log.anchoring is Anchoring.ANCHORED and self._verifier.anchoring is Anchoring.ANCHORED
                and self._registry_log is not None):
            return Anchoring.ANCHORED
        return Anchoring.UNANCHORED

    def _require_anchored(self) -> None:
        if self.anchoring is not Anchoring.ANCHORED:
            raise TrainingJobError(ANCHOR_REQUIRED)

    def _registry_decision_ids(self) -> frozenset[str]:
        if self._registry_log is None:
            return frozenset()
        try:
            return decision_ids(self._registry_log, anchor=self._log.anchor, log_name=self._registry_log_name)
        except ChainedLogError as exc:
            raise TrainingJobError(exc.code) from None

    def _verify_foreign_heads(self) -> None:
        """The registry log and the consumed ledger sit exactly at their anchored heads."""
        self._registry_decision_ids()
        try:
            self._verifier.ledger.verify_head()
        except DecisionError as error:
            raise TrainingJobError(error.code.value) from None

    def verify_heads(self) -> None:
        """Every log behind a run approval (jobs, registry, consumed ledger) is anchored and at its anchored
        head; ``ANCHOR_REQUIRED``, ``LEDGER_ROLLBACK_DETECTED`` or ``LEDGER_HEAD_NOT_ANCHORED`` otherwise."""
        self._require_anchored()
        self.all()
        self._verify_foreign_heads()

    def approval_subject(self, job_id: str) -> str:
        """What the operator signs to approve running ``job_id`` (operator tool convenience)."""
        return run_approval_subject(self.get(job_id))

    def all(self) -> dict[str, TrainingJob]:
        try:
            return replay(self._log.entries())
        except ChainedLogError as exc:
            raise TrainingJobError(exc.code) from None

    def get(self, job_id: str) -> TrainingJob:
        job = self.all().get(job_id)
        if job is None:
            raise TrainingJobError("JOB_NOT_FOUND")
        return job

    def used_decision_ids(self) -> frozenset[str]:
        return frozenset(job.approval_id for job in self.all().values() if job.approval_id)

    def _base(self, job: TrainingJob, event: JobEvent, target: JobState) -> dict[str, Any]:
        return {"job_id": job.job_id, "event": event.value, "from_state": job.state.value, "to_state": target.value,
                "contract_hash": job.contract_hash, "kind": job.kind, "backend_adapter_id": job.backend_adapter_id,
                "dataset_hash": job.dataset_hash, "approval_id": None, "decision": None, "artifact_sha256": None,
                "metrics_hash": None, "failure_code": None, "at": self._clock().astimezone(UTC).isoformat()}

    def _append(self, build: Callable[[list[dict[str, Any]]], dict[str, Any] | None]) -> None:
        try:
            self._log.append(build)
        except ChainedLogError as exc:
            raise TrainingJobError(exc.code) from None

    def _record(self, job_id: str, event: JobEvent, check: Callable[[TrainingJob], dict[str, Any]]) -> TrainingJob:
        def build(entries: list[dict[str, Any]]) -> dict[str, Any]:
            job = replay(entries).get(job_id)
            if job is None:
                raise TrainingJobError("JOB_NOT_FOUND")
            target = transition(job.state, _EVENT_TARGET[event])
            return {**self._base(job, event, target), **check(job)}

        self._append(build)
        return self.get(job_id)

    def create(self, contract: TrainingContract, *, job_id: str | None = None) -> TrainingJob:
        contract = check_contract(contract)
        job_id = job_id or f"job-{secrets.token_hex(12)}"
        if not _JOB_ID.fullmatch(job_id):
            raise TrainingJobError("JOB_ID_INVALID")

        def build(entries: list[dict[str, Any]]) -> dict[str, Any]:
            if job_id in replay(entries):
                raise TrainingJobError("JOB_ALREADY_EXISTS")
            return {"job_id": job_id, "event": JobEvent.CREATED.value, "from_state": None,
                    "to_state": JobState.DRAFT.value, "contract_hash": contract.contract_hash,
                    "kind": contract.kind.value, "backend_adapter_id": contract.backend_adapter_id,
                    "dataset_hash": contract.dataset.content_hash, "approval_id": None, "decision": None,
                    "artifact_sha256": None, "metrics_hash": None, "failure_code": None,
                    "at": self._clock().astimezone(UTC).isoformat()}

        self._append(build)
        return self.get(job_id)

    def approve(self, job_id: str, record: Mapping[str, Any]) -> TrainingJob:
        """Consume a signed APPROVE_TRAINING_RUN decision bound to exactly this job (``run_approval_subject``).

        Idempotent: the same decision replayed for the same job changes nothing. Requires the job log, the
        registry log and the consumed ledger to be anchored (``ANCHOR_REQUIRED``)."""
        self._require_anchored()
        try:
            retained = retained_record(record)
        except DecisionError as error:
            raise TrainingJobError(f"RUN_APPROVAL_REFUSED:{error.code.value}") from None
        def build(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
            jobs = replay(entries)
            job = jobs.get(job_id)
            if job is None:
                raise TrainingJobError("JOB_NOT_FOUND")
            if job.approval_id == retained["decision_id"] and job.approval_decision == retained:
                return None  # the same decision replayed for the same job: nothing changes
            target = transition(job.state, JobState.APPROVED_TO_RUN)
            used = {j.approval_id for j in jobs.values() if j.approval_id} | self._registry_decision_ids()
            if retained["decision_id"] in used:
                raise TrainingJobError("DECISION_ALREADY_USED")
            binding = DecisionBinding(DecisionOperation.APPROVE_TRAINING_RUN, run_approval_subject(job))
            outcome = self._verifier.consume(retained, binding, now=self._clock())
            if not outcome.ok or outcome.decision_id is None:
                raise TrainingJobError(f"RUN_APPROVAL_REFUSED:{outcome.code.value}")
            return {**self._base(job, JobEvent.APPROVED, target), "approval_id": outcome.decision_id,
                    "decision": retained}

        self._append(build)
        return self.get(job_id)

    def approval_verified(self, job: TrainingJob) -> bool:
        """Re-verify the retained run approval (MAC, binding, window) and its consumed-ledger record.

        ``job`` must equal the job replayed now from the anchored log, and every log must sit at its
        anchored head (a rollback raises ``TrainingJobError`` with the anchor code). Unanchored logs never
        verify."""
        if self.anchoring is not Anchoring.ANCHORED:
            return False
        if self.all().get(job.job_id) != job:
            return False
        return self._approval_verified(job)

    def _approval_verified(self, job: TrainingJob) -> bool:
        """``approval_verified`` for a job replayed from entries already read under the job-log lock."""
        if self.anchoring is not Anchoring.ANCHORED:
            return False
        if job.approval_decision is None or job.approved_at is None or job.approval_id is None:
            return False
        self._verify_foreign_heads()
        try:
            binding = DecisionBinding(DecisionOperation.APPROVE_TRAINING_RUN, run_approval_subject(job))
            at = parse_time(job.approved_at)
        except (TrainingJobError, ValueError):
            return False
        outcome = self._verifier.verify(dict(job.approval_decision), binding, at=at)
        try:
            return (outcome.ok and outcome.decision_id == job.approval_id
                    and self._verifier.consumed_matches(dict(job.approval_decision), binding))
        except DecisionError as error:  # rollback below the anchored ledger head: fail closed, loudly
            raise TrainingJobError(error.code.value) from None

    def start(self, job_id: str, *, approval_id: str) -> TrainingJob:
        """Starting needs the explicit approval reference and a retained approval that still verifies."""

        self._require_anchored()

        def check(job: TrainingJob) -> dict[str, Any]:
            if job.approval_id is None or approval_id != job.approval_id:
                raise TrainingJobError("RUN_APPROVAL_REFERENCE_MISMATCH")
            if not self._approval_verified(job):
                raise TrainingJobError("RUN_APPROVAL_UNVERIFIED")
            return {"approval_id": approval_id}

        return self._record(job_id, JobEvent.STARTED, check)

    def complete(self, job_id: str, result: TrainingRunResult) -> TrainingJob:
        def check(job: TrainingJob) -> dict[str, Any]:
            if result.artifact.contract_hash != job.contract_hash or result.artifact.dataset_hash != job.dataset_hash:
                raise TrainingJobError("RESULT_NOT_BOUND_TO_JOB_CONTRACT")
            return {"artifact_sha256": result.artifact.ref.sha256,
                    "metrics_hash": canonical_hash(dict(result.metrics))}

        return self._record(job_id, JobEvent.COMPLETED, check)

    def fail(self, job_id: str, failure_code: str) -> TrainingJob:
        code = failure_code if _CODE.fullmatch(failure_code) else "TRAINING_FAILED"
        return self._record(job_id, JobEvent.FAILED, lambda job: {"failure_code": code})

    def cancel(self, job_id: str) -> TrainingJob:
        return self._record(job_id, JobEvent.CANCELLED, lambda job: {})


class TrainingRunError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def execute_job(jobs: TrainingJobs, job_id: str, *, contract: TrainingContract, dataset: DatasetSnapshot,
                backend: TrainingBackend, store: ArtifactStore,
                eval_dataset: DatasetSnapshot | None = None) -> TrainingRunResult:
    """Validate, start (explicit, re-verified approval reference), run through a scoped artifact facade, then
    record COMPLETED or FAILED. The result is a candidate artifact; nothing is registered, promoted or deployed."""
    job = jobs.get(job_id)
    if job.state is not JobState.APPROVED_TO_RUN or job.approval_id is None:
        raise TrainingRunError("JOB_NOT_APPROVED_TO_RUN")
    contract = check_contract(contract)
    if contract.contract_hash != job.contract_hash:
        raise TrainingRunError("CONTRACT_NOT_BOUND_TO_JOB")
    descriptor = backend.descriptor
    try:
        validate_contract(contract, dataset, backend_kind=backend.kind, backend_adapter_id=descriptor.adapter_id,
                          backend_license_spdx=descriptor.license.spdx, eval_dataset=eval_dataset)
        backend.validate_contract(contract, dataset)
    except TrainingContractError as exc:
        raise TrainingRunError(exc.code) from None
    if not backend.available():
        raise TrainingRunError("BACKEND_UNAVAILABLE")
    jobs.start(job_id, approval_id=job.approval_id)
    scoped = ScopedArtifacts(store, readable=dataset.members, max_outputs=1, max_output_bytes=MAX_MODEL_OUTPUT_BYTES)
    try:
        result = backend.run(contract, dataset, artifacts=scoped)
    except (TrainingBackendError, TrainingContractError) as exc:
        jobs.fail(job_id, exc.code)
        raise TrainingRunError(exc.code) from None
    except Exception:
        jobs.fail(job_id, "BACKEND_INTERNAL_ERROR")
        raise TrainingRunError("BACKEND_INTERNAL_ERROR") from None
    if not isinstance(result, TrainingRunResult) or result.artifact.ref not in scoped.written:
        jobs.fail(job_id, "BACKEND_OUTPUT_NOT_WRITTEN_THROUGH_SCOPE")
        raise TrainingRunError("BACKEND_OUTPUT_NOT_WRITTEN_THROUGH_SCOPE")
    if result.artifact.backend_adapter_id != descriptor.adapter_id:
        jobs.fail(job_id, "BACKEND_OUTPUT_IDENTITY_MISMATCH")
        raise TrainingRunError("BACKEND_OUTPUT_IDENTITY_MISMATCH")
    try:
        jobs.complete(job_id, result)
    except TrainingJobError as exc:
        jobs.fail(job_id, "RESULT_NOT_BOUND_TO_JOB_CONTRACT")
        raise TrainingRunError(exc.code) from None
    return result
