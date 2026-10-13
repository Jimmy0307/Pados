"""Shared fixtures for the Training Plane tests (no network, no database, no ambient environment)."""
from __future__ import annotations

import hashlib
import json
import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import SecretBoundary, SecretValue
from pados.execution.context import canonical_hash
from pados.training.backends import AlgorithmFitBackend, TrainingRunResult
from pados.training.contracts import (
    DatasetProvenance,
    DatasetSnapshot,
    LicenseConstraints,
    TrainingContract,
    TrainingKind,
)
from pados.training._chain import ANCHOR_FIELDS, ANCHOR_SCHEMA, GENESIS, ExternalAnchor, FileExternalAnchor
from pados.training.backends import default_backends
from pados.training.decisions import (
    ConsumedDecisionLedger,
    DecisionOperation,
    DecisionVerifier,
    TrainingOperatorRegistry,
)
from pados.training.evaluation import EvaluationReport, evaluate_algorithm_fit
from pados.training.jobs import TrainingJobs, execute_job
from pados.training.operator import ConfirmationRequest, DecisionSigner, OperatorSession, open_operator_signer
from pados.training.registry import ModelRegistry, ModelRegistryCandidate, PromotionGate, build_candidate

APPROVER = "alice.operator"
SECOND_APPROVER = "carol.operator"
COMMIT = hashlib.sha1(b"pados-training-test").hexdigest()
START = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CREATED = START.isoformat()
# The operator allows the PADOS-owned backend (AGPL-3.0-only, the Core license) and permissive third-party engines.
LICENSES = LicenseConstraints(allowed_dataset_spdx=("CC-BY-4.0", "Apache-2.0"),
                              allowed_backend_spdx=("AGPL-3.0-only", "Apache-2.0"),
                              allowed_base_model_spdx=("Apache-2.0",), output_spdx="Apache-2.0")


class FixedResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, ref: str) -> SecretValue | None:
        value = self._values.get(ref)
        return SecretValue(value) if value else None


class Clock:
    """Deterministic clock: advances one second per reading."""

    def __init__(self, start: datetime = START) -> None:
        self.current = start

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


def linear_rows(count: int, *, seed: int, offset: float = 0.0) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rows = []
    for _ in range(count):
        x1, x2 = rng.uniform(-5, 5) + offset, rng.uniform(-5, 5)
        rows.append({"features": [round(x1, 6), round(x2, 6)],
                     "label": round(2.0 + 3.0 * x1 - 1.5 * x2 + rng.gauss(0, 0.05), 6)})
    return rows


def binary_rows(count: int, *, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rows = []
    for _ in range(count):
        x1, x2 = rng.uniform(-3, 3), rng.uniform(-3, 3)
        label = 1 if x1 + 0.5 * x2 + rng.gauss(0, 0.3) > 0 else 0
        rows.append({"features": [round(x1, 6), round(x2, 6)], "label": label})
    return rows


def put_rows(store: ObjectStoreArtifacts, rows: list[dict[str, Any]]) -> ArtifactRef:
    payload = "\n".join(json.dumps(row, sort_keys=True) for row in rows).encode("utf-8")
    return store.put(payload, media_type="application/x-ndjson", kind="training.records")


def make_snapshot(store: ObjectStoreArtifacts, chunks: list[list[dict[str, Any]]], *, snapshot_id: str,
                  classification: DataClassification = DataClassification.INTERNAL,
                  license_spdx: str = "CC-BY-4.0") -> DatasetSnapshot:
    members = tuple(put_rows(store, chunk) for chunk in chunks)
    return DatasetSnapshot.build(
        snapshot_id=snapshot_id, schema_ref="training.tabular/v1", members=members,
        record_count=sum(len(chunk) for chunk in chunks), classification=classification, license_spdx=license_spdx,
        provenance=DatasetProvenance(source_ref="project.fixture", collection_method="OPERATOR_CURATED"),
        created_at=CREATED)


def make_contract(train: DatasetSnapshot, held_out: DatasetSnapshot | None = None, **overrides: Any) -> TrainingContract:
    values: dict[str, Any] = dict(
        contract_id="contract-1", kind=TrainingKind.ALGORITHM_FIT, dataset=train.ref(),
        eval_dataset=None if held_out is None else held_out.ref(),
        config={"algorithm": "OLS", "feature_count": 2}, backend_adapter_id=AlgorithmFitBackend.ADAPTER_ID,
        code_version=COMMIT, seed=7, compute_resource_ref="resource.local-cpu",
        classification=DataClassification.INTERNAL, license_constraints=LICENSES)
    values.update(overrides)
    return TrainingContract(**values)


def key_ref(operator_id: str) -> str:
    """Each approver has their own key reference (CR-12b TP-02a)."""
    return f"secretref://training/operators/{operator_id}"


KEY_REF = key_ref(APPROVER)


def os_account(operator_id: str) -> dict[str, Any]:
    """Each approver is bound to their own (fictitious, test-only) POSIX account (CR-12c TP-02)."""
    uid = 40000 + int(hashlib.sha256(operator_id.encode()).hexdigest(), 16) % 10000
    return {"platform": "posix", "uid": uid, "login": operator_id.split(".")[0]}


def operator_entry(operator_id: str, *, enabled: bool = True) -> dict[str, Any]:
    return {"operator_id": operator_id, "enabled": enabled, "key_ref": key_ref(operator_id),
            "os_account": os_account(operator_id)}


def operator_registry(operators: tuple[str, ...] = (APPROVER,)) -> TrainingOperatorRegistry:
    return TrainingOperatorRegistry.from_document({
        "schema": "pados.training-operators/v3", "operators": [operator_entry(o) for o in operators]})


def session_for(operator_id: str, *, interactive: bool = True) -> OperatorSession:
    account = os_account(operator_id)
    return OperatorSession("posix", account["login"], account["uid"], None, interactive)


def correct_reply(request: ConfirmationRequest) -> str:
    """What an attentive human types: the operation and the first 12 hex characters of the subject."""
    return f"{request.operation.value} {request.subject_hash[:12]}"


@dataclass
class Terminal:
    """Stands in for the operator's OS login and controlling terminal (the private seams of
    ``pados.training.operator``), installed for every training test by ``conftest.py``."""

    session: OperatorSession
    reply: Callable[[ConfirmationRequest], str] = correct_reply
    prompts: list[ConfirmationRequest] = field(default_factory=list)

    def login(self, operator_id: str) -> None:
        self.session = session_for(operator_id, interactive=self.session.interactive)

    def current_session(self) -> OperatorSession:
        return self.session

    def read(self, request: ConfirmationRequest) -> str:
        self.prompts.append(request)
        return self.reply(request)


TERMINAL: list[Terminal] = []


def terminal() -> Terminal:
    return TERMINAL[-1]


@dataclass
class Keys:
    """Operator-side key material for the tests (resolved only through SecretBoundary by the code under test)."""

    values: dict[str, str]
    boundary: SecretBoundary
    registry: TrainingOperatorRegistry

    def signer(self, approver: str = APPROVER) -> DecisionSigner:
        terminal().login(approver)  # the approver signs from their own OS login
        return open_operator_signer(approver=approver, boundary=self.boundary, registry=self.registry)


def make_keys(*, key: str | None = None, operators: tuple[str, ...] = (APPROVER, SECOND_APPROVER)) -> Keys:
    values = {key_ref(o): secrets.token_hex(32) for o in operators}
    if key is not None and operators:
        values[key_ref(operators[0])] = key
    return Keys(values, SecretBoundary(FixedResolver(values)), operator_registry(operators))


def make_pair(tmp: Path, *, key: str | None = None, operators: tuple[str, ...] = (APPROVER,),
              ledger_name: str = "decisions.jsonl") -> tuple[DecisionSigner, DecisionVerifier]:
    """Operator-side signer (first operator) and a training-side verifier over the same operator registry."""
    keys = make_keys(key=key, operators=operators)
    verifier = DecisionVerifier(boundary=keys.boundary, registry=keys.registry,
                                ledger=ConsumedDecisionLedger(tmp / ledger_name))
    return keys.signer(operators[0]), verifier


@dataclass
class Env:
    tmp: Path
    clock: Clock
    store: ObjectStoreArtifacts
    signer: DecisionSigner
    verifier: DecisionVerifier
    jobs: TrainingJobs
    registry: ModelRegistry
    gate: PromotionGate
    backend: AlgorithmFitBackend
    train: DatasetSnapshot
    held_out: DatasetSnapshot
    key: str
    keys: Keys
    anchor: ExternalAnchor | None = None

    def contract(self, **overrides: Any) -> TrainingContract:
        return make_contract(self.train, self.held_out, **overrides)

    def decision(self, operation: DecisionOperation, subject_hash: str, **overrides: Any) -> dict[str, Any]:
        """Sign as ``approver`` (default alice) with that approver's own key through the operator entry point."""
        approver = overrides.pop("approver", APPROVER)
        signer = self.signer if approver == APPROVER else self.keys.signer(approver)
        values: dict[str, Any] = dict(operation=operation, subject_hash=subject_hash, issued_at=START,
                                      lifetime=timedelta(hours=1))
        values.update(overrides)
        terminal().login(approver)
        return signer.issue(**values)

    def run_subject(self, job_id: str) -> str:
        return self.jobs.approval_subject(job_id)

    def subject(self, candidate: ModelRegistryCandidate) -> str:
        return self.registry.decision_subject(candidate.candidate_hash)

    def approved_job(self, contract: TrainingContract, job_id: str = "job-00000001") -> str:
        self.jobs.create(contract, job_id=job_id)
        self.jobs.approve(job_id, self.decision(DecisionOperation.APPROVE_TRAINING_RUN, self.run_subject(job_id)))
        return job_id

    def trained(self, contract: TrainingContract | None = None,
                job_id: str = "job-00000001") -> tuple[str, TrainingContract, TrainingRunResult]:
        contract = contract or self.contract()
        self.approved_job(contract, job_id)
        result = execute_job(self.jobs, job_id, contract=contract, dataset=self.train, backend=self.backend,
                             store=self.store,
                             eval_dataset=self.held_out if contract.eval_dataset is not None else None)
        return job_id, contract, result

    def report(self, result: TrainingRunResult, report_id: str = "report-1",
               held_out: DatasetSnapshot | None = None) -> EvaluationReport:
        return evaluate_algorithm_fit(result.artifact.ref, store=self.store, eval_dataset=held_out or self.held_out,
                                      training_dataset=self.train, report_id=report_id, created_at=CREATED)

    def candidate(self, *, evaluate: bool = True, job_id: str = "job-00000001",
                  contract: TrainingContract | None = None) -> tuple[ModelRegistryCandidate, TrainingContract]:
        job_id, contract, result = self.trained(contract, job_id)
        candidate = build_candidate(candidate_id=f"cand-{job_id}", job=self.jobs.get(job_id), result=result,
                                    contract=contract, dataset=self.train, backend=self.backend, store=self.store,
                                    eval_dataset=self.held_out if evaluate else None, created_at=CREATED)
        return candidate, contract

    def register(self, candidate: ModelRegistryCandidate, contract: TrainingContract, **overrides: Any) -> Any:
        values: dict[str, Any] = dict(contract=contract, dataset=self.train, eval_dataset=self.held_out,
                                      registered_by="bob.engineer")
        values.update(overrides)
        return self.registry.register(candidate, **values)

    def registered(self, **kwargs: Any) -> tuple[ModelRegistryCandidate, TrainingContract]:
        candidate, contract = self.candidate(**kwargs)
        self.register(candidate, contract)
        return candidate, contract


def anchor_dir(tmp: Path) -> Path:
    """A sibling of the log store (never inside it), standing in for operator-held anchor storage."""
    return tmp.with_name(tmp.name + "-anchor")


def make_env(tmp: Path, *, anchored: bool = True, clock: Clock | None = None,
             anchor: ExternalAnchor | None = None, keys: Keys | None = None) -> Env:
    """Training plane over ``tmp``; anchored (mandatory for authority) unless ``anchored=False``."""
    clock = clock or Clock()
    tmp.mkdir(parents=True, exist_ok=True)
    store = ObjectStoreArtifacts(tmp / "objects")
    keys = keys or make_keys()
    if anchor is None and anchored:
        anchor = FileExternalAnchor(anchor_dir(tmp))
    if isinstance(anchor, FileExternalAnchor):
        anchor.directory.mkdir(parents=True, exist_ok=True)
    verifier = DecisionVerifier(boundary=keys.boundary, registry=keys.registry,
                                ledger=ConsumedDecisionLedger(tmp / "decisions.jsonl", anchor=anchor))
    jobs = TrainingJobs(tmp / "jobs.jsonl", verifier=verifier, clock=clock, registry_log=tmp / "registry.jsonl",
                        anchor=anchor)
    registry = ModelRegistry(tmp / "registry.jsonl", jobs=jobs, verifier=verifier, backends=default_backends(),
                             store=store, clock=clock, anchor=anchor)
    train = make_snapshot(store, [linear_rows(40, seed=1), linear_rows(40, seed=2)], snapshot_id="train-1")
    held_out = make_snapshot(store, [linear_rows(30, seed=3)], snapshot_id="heldout-1")
    return Env(tmp, clock, store, keys.signer(APPROVER), verifier, jobs, registry, PromotionGate(registry),
               AlgorithmFitBackend(), train, held_out, keys.values[key_ref(APPROVER)], keys, anchor)


LOG_NAMES = ("jobs.jsonl", "registry.jsonl", "decisions.jsonl")
ANCHOR_NAMES = {"jobs.jsonl": "training-job-events", "registry.jsonl": "model-registry",
                "decisions.jsonl": "training-decision-ledger"}


def snapshot_logs(tmp: Path) -> dict[str, bytes | None]:
    """Byte-exact copy of every training log (what an attacker keeps to roll the store back later)."""
    return {name: (tmp / name).read_bytes() if (tmp / name).exists() else None for name in LOG_NAMES}


def restore_logs(tmp: Path, saved: dict[str, bytes | None], *, anchor_too: bool = False) -> None:
    """Coordinated rollback of every training log to an earlier, internally consistent snapshot.

    ``anchor_too`` also rolls the anchor store back (an attacker who controls it as well)."""
    for name, data in saved.items():
        if data is None:
            (tmp / name).unlink(missing_ok=True)
        else:
            (tmp / name).write_bytes(data)
        if anchor_too:
            reanchor(tmp / name)


def reanchor(path: Path) -> None:
    """Move the anchored head to the log's current head: an attacker who ALSO controls the anchor store.

    Used to show that the signature layer holds on its own, independently of the anchor."""
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    log_name = ANCHOR_NAMES[path.name]
    head = {"schema": ANCHOR_SCHEMA, "log_name": log_name, "seq": len(entries),
            "head_hash": entries[-1]["entry_hash"] if entries else GENESIS}
    assert set(head) == set(ANCHOR_FIELDS)
    (anchor_dir(path.parent) / f"{log_name}.anchor.json").write_text(json.dumps(head), encoding="utf-8")


def rechain(path: Path, mutate: Callable[[list[dict[str, Any]]], None], *, anchor_too: bool = False) -> None:
    """Rewrite a hash-chained log the way an attacker with file access could (consistent chain).

    ``anchor_too`` models an attacker who can also rewrite the anchor store (``reanchor``)."""
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    mutate(entries)
    previous = "0" * 64
    lines = []
    for number, entry in enumerate(entries, start=1):
        entry.pop("entry_hash", None)
        entry["seq"], entry["prev_hash"] = number, previous
        entry["entry_hash"] = canonical_hash(entry)
        previous = entry["entry_hash"]
        lines.append(json.dumps(entry, sort_keys=True, separators=(",", ":")))
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    if anchor_too:
        reanchor(path)
